from typing import Any

from redis.asyncio import Redis

from db.base import AsyncCache

redis: Redis | None = None


class RedisCache(AsyncCache):
    """Реализация `AsyncCache` поверх `redis.asyncio.Redis`.

    SRP: класс отвечает только за хранение/получение значений в Redis.
    DIP: сервисы зависят от абстракции `AsyncCache`, а не от Redis напрямую.
    OCP: для замены backend'а достаточно реализовать `AsyncCache` (например,
    `InMemoryCache`, `MemcachedCache`) — менять сервисы не потребуется.
    """

    def __init__(self, redis: Redis):
        self.redis = redis

    async def get(self, key: str) -> Any | None:
        return await self.redis.get(key)

    async def set(self, key: str, value: Any, expire: int | None = None) -> None:
        await self.redis.set(key, value, ex=expire)


# Функция понадобится при внедрении зависимостей
def get_redis() -> Redis:
    return redis
