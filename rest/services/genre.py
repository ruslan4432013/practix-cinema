from functools import lru_cache

from fastapi import Depends

from core.config import settings
from db.base import AsyncCache, AsyncDataStorage
from db.elastic_db import ElasticsearchStorage, get_elastic
from db.redis_db import RedisCache, get_redis
from models.film import Genre
from services import queries
from services.base import BaseService


class GenreService(BaseService[Genre]):
    index = settings.es_genres_index
    cache_prefix = 'genre'
    model = Genre

    async def get_list(self) -> list[Genre]:
        cache_key = 'genres:all'
        cached = await self.cache.get_list(cache_key)
        if cached is not None:
            return cached

        result = await self.storage.search(index=self.index, body=queries.genres_all_query())
        genres = [Genre(**hit['_source']) for hit in result['hits']['hits']]

        await self.cache.set_list(cache_key, genres)
        return genres


@lru_cache
def get_genre_service(
    cache: AsyncCache = Depends(get_redis),
    storage: AsyncDataStorage = Depends(get_elastic),
) -> GenreService:
    return GenreService(RedisCache(cache), ElasticsearchStorage(storage))
