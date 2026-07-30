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
from ipaddress import ip_address

from user_agents import parse as parse_user_agent

from core.config import settings
from models.enums import DeviceType

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
    """Пришло ли соединение от известного прокси.

    Пустой список доверенных сетей означает «доверять всем» и допустим только
    в тестах, где приложение поднимается in-process и адреса соединения нет.
    """
    networks = settings.trusted_proxy_networks
    if not networks:
        return True
    if not peer:
        return False
    try:
        address = ip_address(peer)
    except ValueError:
        return False
    return any(address in network for network in networks)


def get_client_ip(headers: dict[str, str], peer: str | None) -> str | None:
    """Определяет IP клиента с учётом того, что сервис стоит за Nginx.

    За обратным прокси адрес соединения — это адрес самого прокси, поэтому
    адрес берётся из заголовка. Порядок: ``X-Real-IP``, затем ПОСЛЕДНИЙ элемент
    ``X-Forwarded-For``, и лишь в последнюю очередь адрес соединения.

    ЗАГОЛОВКАМ ДОВЕРЯЕМ ТОЛЬКО ОТ ДОВЕРЕННОГО ПРОКСИ. Иначе клиент, обратившийся
    к сервису напрямую, подделал бы заголовок, получил бы новый счётчик rate
    limit на каждый запрос и произвольно накрутил бы ``ip_hash`` в аналитике.

    ПОЧЕМУ НЕ ПЕРВЫЙ ЭЛЕМЕНТ ``X-Forwarded-For``. Nginx формирует заголовок как
    ``$proxy_add_x_forwarded_for`` — это присланный клиентом XFF ПЛЮС дописанный
    справа реальный адрес. Слева, таким образом, стоит ровно то, что подставил
    сам клиент: беря первый элемент, сервис доверял бы подделке, хотя проверка
    ``is_trusted_proxy`` формально проходила (соединение-то от Nginx). Nginx уже
    вычислил настоящий адрес — ``set_real_ip_from`` + ``real_ip_recursive``
    отбрасывают доверенные хопы, и результат попадает и в ``X-Real-IP``, и в
    правый край XFF. Поэтому доверяем именно им.

    Последствие обратной ошибки известно и учтено: если добавить перед Nginx ещё
    один прокси и не внести его в ``set_real_ip_from``, все клиенты получат его
    адрес и сольются в один счётчик лимита. Это чинится настройкой доверенных
    сетей, а не возвратом к первому элементу заголовка.
    """
    if not is_trusted_proxy(peer):
        return peer

    real_ip = (headers.get('x-real-ip') or '').strip()
    if real_ip:
        return real_ip

    forwarded_for = headers.get('x-forwarded-for') or ''
    hops = [hop.strip() for hop in forwarded_for.split(',') if hop.strip()]
    if hops:
        # Самый правый — тот, что дописал доверенный прокси. Всё левее прислал
        # клиент, и проверять это бессмысленно: он волен написать там что угодно.
        return hops[-1]
    return peer
