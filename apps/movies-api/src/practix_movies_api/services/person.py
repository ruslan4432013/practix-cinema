from functools import lru_cache

from fastapi import Depends

from practix_movies_api.core.config import settings
from practix_movies_api.db.base import AsyncCache, AsyncDataStorage
from practix_movies_api.db.elastic_db import ElasticsearchStorage, get_elastic
from practix_movies_api.db.redis_db import RedisCache, get_redis
from practix_movies_api.models.person import PersonES, PersonFilmES
from practix_movies_api.services import queries
from practix_movies_api.services.base import BaseService


class PersonService(BaseService[PersonES]):
    index = settings.es_persons_index
    cache_prefix = 'person'
    model = PersonES

    async def get_by_id(self, entity_id: str) -> PersonES | None:
        key = f'{self.cache_prefix}:{entity_id}'
        entity = await self.cache.get(key)
        if entity:
            return entity
        entity = await self._get_from_storage(entity_id)
        if not entity:
            return None
        entity.films = await self._get_person_films(entity_id)
        await self.cache.set(key, entity)
        return entity

    async def search(self, query: str, page_number: int, page_size: int) -> list[PersonES]:
        cache_key = f'persons:search:{query}:{page_number}:{page_size}'
        cached = await self.cache.get_list(cache_key)
        if cached is not None:
            return cached

        body = queries.persons_search_query(query, page_number, page_size)
        result = await self.storage.search(index=self.index, body=body)

        persons: list[PersonES] = []
        for hit in result['hits']['hits']:
            person = PersonES(**hit['_source'])
            person.films = await self._get_person_films(str(person.id))
            persons.append(person)

        await self.cache.set_list(cache_key, persons)
        return persons

    async def get_list(self, page_number: int, page_size: int) -> list[PersonES]:
        cache_key = f'persons:list:{page_number}:{page_size}'
        cached = await self.cache.get_list(cache_key)
        if cached is not None:
            return cached

        body = queries.persons_list_query(page_number, page_size)
        result = await self.storage.search(index=self.index, body=body)

        persons: list[PersonES] = []
        for hit in result['hits']['hits']:
            person = PersonES(**hit['_source'])
            person.films = await self._get_person_films(str(person.id))
            persons.append(person)

        await self.cache.set_list(cache_key, persons)
        return persons

    async def get_films_by_person(self, person_id: str) -> list[dict] | None:
        person = await self._get_from_storage(person_id)
        if not person:
            return None

        body = queries.person_films_short_query(person_id)
        result = await self.storage.search(index=settings.es_movies_index, body=body)
        return [
            {
                'uuid': hit['_source']['id'],
                'title': hit['_source'].get('title', ''),
                'imdb_rating': hit['_source'].get('imdb_rating'),
            }
            for hit in result['hits']['hits']
        ]

    async def _get_person_films(self, person_id: str) -> list[PersonFilmES]:
        body = queries.person_films_roles_query(person_id)
        result = await self.storage.search(index=settings.es_movies_index, body=body)
        films: list[PersonFilmES] = []
        for hit in result['hits']['hits']:
            src = hit['_source']
            roles: list[str] = []
            if any(a.get('id') == person_id for a in src.get('actors', [])):
                roles.append('actor')
            if any(d.get('id') == person_id for d in src.get('directors', [])):
                roles.append('director')
            if any(w.get('id') == person_id for w in src.get('writers', [])):
                roles.append('writer')
            films.append(PersonFilmES(uuid=src['id'], roles=roles))
        return films


@lru_cache
def get_person_service(
    cache: AsyncCache = Depends(get_redis),
    storage: AsyncDataStorage = Depends(get_elastic),
) -> PersonService:
    return PersonService(RedisCache(cache), ElasticsearchStorage(storage))
