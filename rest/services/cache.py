import json

from db.base import AsyncCache

CACHE_EXPIRE_IN_SECONDS = 60 * 5  # 5 минут


class ModelCache[ModelType]:
    """Слой кеширования pydantic-моделей.

    SRP: отвечает только за хранение/чтение моделей в кеше и их (де)сериализацию.
    DIP: зависит от абстракции `AsyncCache`, а не от конкретного backend'а.
    OCP: для смены формата хранения достаточно создать наследника/замену,
    сервисы менять не потребуется.
    """

    def __init__(
        self,
        cache: AsyncCache,
        model: type[ModelType],
        expire: int = CACHE_EXPIRE_IN_SECONDS,
    ):
        self._cache = cache
        self._model = model
        self._expire = expire

    async def get(self, key: str) -> ModelType | None:
        data = await self._cache.get(key)
        if not data:
            return None
        return self._model.model_validate_json(data)

    async def set(self, key: str, obj: ModelType) -> None:
        await self._cache.set(key, obj.model_dump_json(), self._expire)

    async def get_list(self, key: str) -> list[ModelType] | None:
        cached = await self._cache.get(key)
        if not cached:
            return None
        return [self._model.model_validate(item) for item in json.loads(cached)]

    async def set_list(self, key: str, items: list[ModelType]) -> None:
        await self._cache.set(
            key,
            json.dumps([item.model_dump(mode='json') for item in items]),
            self._expire,
        )
