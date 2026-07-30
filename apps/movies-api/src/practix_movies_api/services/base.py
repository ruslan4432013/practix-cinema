from practix_movies_api.db.base import AsyncCache, AsyncDataStorage
from practix_movies_api.services.cache import ModelCache


class BaseService[ModelType]:
    """Базовый сервис: оркестрация чтения сущности (кеш -> хранилище -> кеш).

    SRP: сам не сериализует/не кладёт в Redis и не строит ES-запросы — это
    делегировано `ModelCache` и модулю `services.queries`.
    DIP: зависит от абстракций `AsyncCache` и `AsyncDataStorage`.
    """

    index: str
    cache_prefix: str
    model: type[ModelType]

    def __init__(self, cache: AsyncCache, storage: AsyncDataStorage):
        self.storage = storage
        self.cache = ModelCache(cache, self.model)

    async def get_by_id(self, entity_id: str) -> ModelType | None:
        key = f'{self.cache_prefix}:{entity_id}'
        entity = await self.cache.get(key)
        if entity:
            return entity
        entity = await self._get_from_storage(entity_id)
        if not entity:
            return None
        await self.cache.set(key, entity)
        return entity

    async def _get_from_storage(self, entity_id: str) -> ModelType | None:
        doc = await self.storage.get_by_id(index=self.index, entity_id=entity_id)
        if not doc:
            return None
        return self.model(**doc)
