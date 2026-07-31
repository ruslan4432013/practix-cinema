"""Клиент Redis Auth-сервиса — только для денилиста отозванных токенов.

Своих данных в Redis у сервиса нет: всё состояние в PostgreSQL. Единственная
причина, по которой Redis тут вообще есть, — проверка отозванных токенов, а
денилист ведёт Auth, и читать его надо ТАМ ЖЕ, ГДЕ ОН ПИШЕТСЯ (база 0). Именно
на этом коллектор однажды обжёгся: он искал ключи денилиста в своей базе, где их
не было, и отозванный токен считался действительным.

Таймауты короткие и обязательные. Проверка денилиста стоит на горячем пути
каждого запроса с токеном, а «чёрная дыра» вместо Redis (пакеты дропаются, RST не
приходит) без ``socket_timeout`` подвесила бы и запрос, и пробу готовности,
которая тот же клиент пингует.
"""

import asyncio

from redis.asyncio import Redis

from practix_ugc_api.core.config import settings

auth_redis: Redis | None = None


def create_auth_redis() -> Redis:
    return Redis(
        host=settings.auth_redis_host,
        port=settings.auth_redis_port,
        db=settings.AUTH_REDIS_DB,
        socket_timeout=settings.UGC_API_REDIS_TIMEOUT,
        socket_connect_timeout=settings.UGC_API_REDIS_TIMEOUT,
        decode_responses=True,
    )


async def get_auth_redis() -> Redis | None:
    return auth_redis


async def denylist_reachable(client: Redis | None) -> bool:
    """Отвечает ли Redis денилиста. Общая проверка для старта и пробы готовности.

    Клиента нет — тот же исход, что и при ошибке: загрузчик денилиста при
    ``on_error='deny'`` обе ветки трактует как «токен отозван»
    (``practix_core.jwt``).

    Собственный лимит времени, а не только ``socket_connect_timeout`` клиента:
    redis-py повторяет неудачное соединение (по умолчанию три раза с джиттером),
    и на старте это растянуло бы запуск на секунды при недоступном Redis.
    """
    if client is None:
        return False
    try:
        async with asyncio.timeout(settings.UGC_API_REDIS_TIMEOUT * 3):
            await client.ping()
    except Exception:  # noqa: BLE001 — любая ошибка здесь означает «денилист недоступен»
        return False
    return True
