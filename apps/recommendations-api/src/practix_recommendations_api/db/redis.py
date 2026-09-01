"""Два клиента Redis, и разделение между ними — не мелочь.

``recs_redis`` — горячий слой витрины, СВОЙ экземпляр (``redis-recs``) с
политикой ``volatile-lru``: ключи версии живут по TTL, и именно TTL убирает
старую версию батча после переключения указателя.

``auth_redis`` — ЧУЖОЙ экземпляр: денилист отозванных токенов ведёт Auth, и
читать его надо там же, где он пишется (база 0 общего ``redis``). Ровно на этом
однажды обжёгся коллектор: он искал ключи денилиста в своей базе, где их не
было, и отозванный токен считался действительным.

Один клиент на оба назначения означал бы либо денилист не в той базе, либо
вытеснение сессий Auth под давлением витрины — то есть авария в рекомендациях
роняла бы вход на сайт.

Таймауты короткие и обязательные: оба клиента стоят на горячем пути, а «чёрная
дыра» вместо Redis (пакеты дропаются, RST не приходит) без ``socket_timeout``
подвесила бы и запрос, и пробу готовности, которая тот же клиент пингует.

Проверка доступности живёт в ``practix_core.health.reachable``: она нужна и
здесь, и в ugc-api, и была бы третьей копией одной и той же десятистрочной
функции.
"""

from redis.asyncio import Redis

from practix_recommendations_api.core.config import settings

recs_redis: Redis | None = None
auth_redis: Redis | None = None


def create_recs_redis() -> Redis:
    return Redis(
        host=settings.RECS_REDIS_HOST,
        port=settings.RECS_REDIS_PORT,
        db=settings.RECS_REDIS_DB,
        socket_timeout=settings.RECS_REDIS_TIMEOUT,
        socket_connect_timeout=settings.RECS_REDIS_TIMEOUT,
        decode_responses=True,
    )


def create_auth_redis() -> Redis:
    return Redis(
        host=settings.auth_redis_host,
        port=settings.auth_redis_port,
        db=settings.AUTH_REDIS_DB,
        socket_timeout=settings.RECS_AUTH_REDIS_TIMEOUT,
        socket_connect_timeout=settings.RECS_AUTH_REDIS_TIMEOUT,
        decode_responses=True,
    )


async def get_recs_redis() -> Redis | None:
    return recs_redis


async def get_auth_redis() -> Redis | None:
    return auth_redis
