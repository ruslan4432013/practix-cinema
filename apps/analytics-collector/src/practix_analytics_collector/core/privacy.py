"""Минимизация персональных данных.

Сервис аналитики по своей природе видит чувствительные данные: IP-адрес,
User-Agent, историю перемещений по сайту. Модуль отвечает за то, чтобы в Kafka
(а значит — в долговременное хранилище и во все дашборды) попадало ровно
столько, сколько нужно для аналитики, и ни байтом больше.

Ключевое решение: **сырой IP не покидает процесс**. В конверт события уходит
только ``blake2b(ip, key=salt)``. Такой хеш позволяет отличать клиентов друг от
друга и считать уникальных посетителей, но не восстанавливать адрес. Соль
обязательна: без неё пространство IPv4 (~4 млрд значений) перебирается радужной
таблицей за минуты, и «хеш» не даёт никакой защиты.
"""

import hashlib

from user_agents import parse as parse_user_agent

from practix_analytics_collector.core.config import settings
from practix_analytics_collector.models.enums import DeviceType
from practix_core.net import is_trusted_proxy as _is_trusted_proxy
from practix_core.net import resolve_client_ip

# Длина хеша в байтах. 16 байт (32 hex-символа) — достаточно, чтобы коллизии
# были пренебрежимы, и вдвое короче полного blake2b в хранилище.
_IP_HASH_DIGEST_SIZE = 16


def hash_ip(ip: str | None) -> str | None:
    """Возвращает необратимый солёный хеш IP-адреса.

    Используется keyed-режим blake2b: соль входит в состояние хеш-функции как
    ключ, а не приписывается к сообщению, что исключает length-extension.
    """
    if not ip:
        return None
    digest = hashlib.blake2b(
        ip.encode('utf-8'),
        key=settings.UGC_IP_HASH_SALT.encode('utf-8'),
        digest_size=_IP_HASH_DIGEST_SIZE,
    )
    return digest.hexdigest()


def truncate_user_agent(user_agent: str | None) -> str | None:
    """Усекает User-Agent до безопасной длины.

    Заголовок приходит от клиента и ничем не ограничен: без усечения одна
    строка могла бы раздуть сообщение в Kafka до мегабайтов.
    """
    if not user_agent:
        return None
    return user_agent[: settings.UGC_MAX_USER_AGENT_LENGTH]


def parse_client_hints(user_agent: str | None) -> tuple[DeviceType, str | None, str | None]:
    """Разбирает User-Agent в (тип устройства, ОС, браузер).

    Разбор выполняется на сервере, а не на клиенте: так значения нормализованы
    единообразно и не подделываются отдельно от самого заголовка. Ошибка
    парсинга не должна ронять приём события, поэтому она подавляется.
    """
    if not user_agent:
        return DeviceType.UNKNOWN, None, None
    try:
        parsed = parse_user_agent(user_agent)
    except Exception:  # noqa: BLE001 — разбор UA не критичен для приёма события
        return DeviceType.UNKNOWN, None, None

    if parsed.is_bot:
        device_type = DeviceType.BOT
    elif parsed.is_tablet:
        device_type = DeviceType.TABLET
    elif parsed.is_mobile:
        device_type = DeviceType.MOBILE
    elif parsed.is_pc:
        device_type = DeviceType.DESKTOP
    else:
        device_type = DeviceType.UNKNOWN

    os_family = parsed.os.family if parsed.os and parsed.os.family != 'Other' else None
    browser_family = parsed.browser.family if parsed.browser and parsed.browser.family != 'Other' else None
    return device_type, os_family, browser_family


def is_trusted_proxy(peer: str | None) -> bool:
    """Пришло ли соединение от известного прокси (сети — из настроек коллектора)."""
    return _is_trusted_proxy(peer, settings.trusted_proxy_networks)


def get_client_ip(headers: dict[str, str], peer: str | None) -> str | None:
    """Определяет IP клиента с учётом того, что сервис стоит за Nginx.

    Алгоритм и его обоснование — в ``practix_core.net.resolve_client_ip``. Для
    коллектора у этой проверки есть второе следствие помимо rate limit: без неё
    клиент подделывал бы свой адрес и накручивал ``ip_hash``, то есть число
    уникальных посетителей в аналитике.
    """
    return resolve_client_ip(headers, peer, settings.trusted_proxy_networks)
