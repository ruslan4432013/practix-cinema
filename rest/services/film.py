from functools import lru_cache

from fastapi import Depends

from core.config import settings
from db.base import AsyncCache, AsyncDataStorage
from db.elastic_db import ElasticsearchStorage, get_elastic
from db.redis_db import RedisCache, get_redis
from models.film import Film
from services import queries
from services.base import BaseService


class FilmService(BaseService[Film]):
    index = settings.es_movies_index
    cache_prefix = 'film'
    model = Film

    async def get_list(
        self,
        sort: str | None,
        genre: str | None,
        page_number: int,
        page_size: int,
        roles: list[str] | None = None,
    ) -> list[Film]:
        """Получение списка фильмов из ES с кэшированием в Redis."""
        cache_key = f'films:list:{sort}:{genre}:{page_number}:{page_size}:{roles}'
        cached = await self.cache.get_list(cache_key)
        if cached is not None:
            return cached

        body = queries.films_list_query(sort, genre, page_number, page_size, roles)
        films = await self._search_films(body)

        await self.cache.set_list(cache_key, films)
        return films

    async def search(self, query: str, page_number: int, page_size: int, roles: list[str] | None = None) -> list[Film]:
        """Поиск фильмов в ES по запросу с кэшированием в Redis."""
        cache_key = f'films:search:{query}:{page_number}:{page_size}:{roles}'
        cached = await self.cache.get_list(cache_key)
        if cached is not None:
            return cached

        body = queries.films_search_query(query, page_number, page_size, roles)
        films = await self._search_films(body)

        await self.cache.set_list(cache_key, films)
        return films

    async def _search_films(self, body: dict) -> list[Film]:
        """Выполнение поиска в Elasticsearch."""
        result = await self.storage.search(index=self.index, body=body)
        return [Film(**hit['_source']) for hit in result['hits']['hits']]


@lru_cache
def get_film_service(
    cache: AsyncCache = Depends(get_redis),
    storage: AsyncDataStorage = Depends(get_elastic),
) -> FilmService:
    return FilmService(RedisCache(cache), ElasticsearchStorage(storage))
