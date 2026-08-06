"""Единственный клиент Redis шлюза — и это ЧУЖОЙ экземпляр.

Шлюз читает денилист отозванных токенов, который ВЕДЁТ Auth, и читать его надо
там же, где он пишется (``AUTH_REDIS_*``, база 0). Ровно на этом однажды обжёгся
коллектор: искал ключи денилиста в своей базе, где их не было, и отозванный
токен считался действующим.

## Почему одноразовые ticket'ы лежат в ТОМ ЖЕ экземпляре

Ticket выдаёт одна реплика, а гасит, вообще говоря, другая — значит, хранилище
обязано быть общим. Из трёх, что есть в стенде, выбран core-redis, и выбор
объясним режимом вытеснения. У core-redis ``maxmemory`` + ``volatile-lru``:
вытесненный ticket означает один лишний round-trip до ручки выдачи — ровно тот
режим отказа, который для тридцатисекундного одноразового ключа правильный. У
``redis-ugc`` стоит ``noeviction``, и он существует для данных, терять которые
нельзя; класть туда заведомо расходный ключ значило бы занимать память,
зарезервированную под недоставленные события аналитики.

Восьмое хранилище ради ключа с TTL в полминуты заводить не за что.

Таймауты короткие и обязательные: «чёрная дыра» вместо Redis (пакеты дропаются,
RST не приходит) без ``socket_timeout`` подвесила бы и handshake, и пробу
готовности, которая тот же клиент пингует.
"""

import asyncio

from redis.asyncio import Redis

from practix_notifications_ws.core.config import settings

client: Redis | None = None


def create_client() -> Redis:
    return Redis(
        host=settings.AUTH_REDIS_HOST,
        port=settings.AUTH_REDIS_PORT,
        db=settings.AUTH_REDIS_DB,
        socket_timeout=settings.NOTIFY_WS_REDIS_TIMEOUT,
        socket_connect_timeout=settings.NOTIFY_WS_REDIS_TIMEOUT,
        decode_responses=True,
    )


async def get_client() -> Redis | None:
    """Провайдер для ``practix_core.jwt.install_denylist_loader`` и зависимостей."""
    return client


async def reachable(redis: Redis | None) -> bool:
    """Отвечает ли Redis. Общая проверка для старта и пробы готовности.

    Собственный лимит времени поверх клиентского: redis-py повторяет неудачное
    соединение (по умолчанию трижды, с джиттером), и на старте это растянуло бы
    запуск на секунды при недоступном Redis.
    """
    if redis is None:
        return False
    try:
        async with asyncio.timeout(settings.NOTIFY_WS_REDIS_TIMEOUT * 3):
            await redis.ping()
    except Exception:  # noqa: BLE001 — любая ошибка здесь означает «недоступен»
        return False
    return True
