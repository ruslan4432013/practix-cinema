"""Клиент Redis Auth-сервиса — только для денилиста отозванных токенов.

Своих данных в Redis у сервиса нет: всё состояние в PostgreSQL. Единственная
причина, по которой Redis тут вообще есть, — проверка отозванных токенов, а
денилист ведёт Auth, и читать его надо ТАМ ЖЕ, ГДЕ ОН ПИШЕТСЯ (база 0). Именно
на этом коллектор однажды обжёгся: он искал ключи денилиста в своей базе, где их
не было, и отозванный токен считался действительным.
"""

from redis.asyncio import Redis

from practix_ugc_api.core.config import settings

auth_redis: Redis | None = None


def create_auth_redis() -> Redis:
    return Redis(
        host=settings.auth_redis_host,
        port=settings.auth_redis_port,
        db=settings.AUTH_REDIS_DB,
        decode_responses=True,
    )


async def get_auth_redis() -> Redis | None:
    return auth_redis
