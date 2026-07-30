from abc import ABC, abstractmethod
from typing import Any


class AsyncCacheReader(ABC):
    @abstractmethod
    async def get(self, key: str) -> Any | None:
        pass


class AsyncCacheWriter(ABC):
    @abstractmethod
    async def set(self, key: str, value: Any, expire: int | None = None) -> None:
        pass


class AsyncCache(AsyncCacheReader, AsyncCacheWriter, ABC):
    """Полный интерфейс кеша (чтение + запись).

    Разделение на `AsyncCacheReader` / `AsyncCacheWriter` соответствует ISP:
    клиенты, которым нужна только часть API, могут зависеть от узкой абстракции.
    """

    pass


class AsyncDataStorage(ABC):
    @abstractmethod
    async def get_by_id(self, index: str, entity_id: str) -> dict | None:
        pass

    @abstractmethod
    async def search(self, index: str, body: dict) -> dict:
        pass
