"""Функциональные тесты для endpoint /api/v1/genres."""

import uuid

import pytest

from practix_testing.utils.helpers import make_genre

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def genres_in_es(es_genres_index, es_write_data, flush_redis):
    """Загружает заранее подготовленный набор жанров в ES."""
    docs = [
        make_genre(name='Action', description='Action movies'),
        make_genre(name='Drama', description='Drama movies'),
        make_genre(name='Comedy', description='Comedy movies'),
        make_genre(name='Thriller', description='Thriller movies'),
    ]
    await es_write_data(docs, es_genres_index)
    return docs


class TestGenresList:
    async def test_list_ok(self, make_get_request, genres_in_es):
        response = await make_get_request('/api/v1/genres')
        assert response.status == 200
        assert isinstance(response.body, list)
        assert len(response.body) == len(genres_in_es)
        for item in response.body:
            assert {'uuid', 'name'} <= set(item.keys())

    async def test_list_has_correct_names(self, make_get_request, genres_in_es):
        response = await make_get_request('/api/v1/genres')
        assert response.status == 200
        names = {item['name'] for item in response.body}
        expected = {g['name'] for g in genres_in_es}
        assert names == expected

    async def test_list_empty_index(self, make_get_request, es_genres_index, flush_redis):
        response = await make_get_request('/api/v1/genres')
        assert response.status == 200
        assert response.body == []


class TestGenreDetails:
    async def test_genre_by_id_ok(self, make_get_request, genres_in_es):
        target = genres_in_es[0]
        response = await make_get_request(f'/api/v1/genres/{target["id"]}')
        assert response.status == 200
        assert response.body['uuid'] == target['id']
        assert response.body['name'] == target['name']

    async def test_genre_not_found(self, make_get_request, genres_in_es):
        response = await make_get_request(f'/api/v1/genres/{uuid.uuid4()}')
        assert response.status == 404

    async def test_genre_invalid_uuid(self, make_get_request, genres_in_es):
        response = await make_get_request('/api/v1/genres/not-a-uuid')
        assert response.status == 422

    async def test_each_genre_accessible(self, make_get_request, genres_in_es):
        for genre in genres_in_es:
            response = await make_get_request(f'/api/v1/genres/{genre["id"]}')
            assert response.status == 200
            assert response.body['uuid'] == genre['id']
            assert response.body['name'] == genre['name']


class TestGenreCache:
    async def test_genre_by_id_cached_in_redis(
        self,
        make_get_request,
        genres_in_es,
        redis_client,
        es_client,
        es_genres_index,
    ):
        target = genres_in_es[0]
        # Первый запрос — наполняем кэш.
        first = await make_get_request(f'/api/v1/genres/{target["id"]}')
        assert first.status == 200

        # Удаляем документ из ES — если кэш работает, ответ всё ещё придёт.
        await es_client.delete(index=es_genres_index, id=target['id'], refresh=True)

        second = await make_get_request(f'/api/v1/genres/{target["id"]}')
        assert second.status == 200
        assert second.body == first.body

    async def test_genres_list_cached_in_redis(
        self,
        make_get_request,
        genres_in_es,
        es_client,
        es_genres_index,
    ):
        # Первый запрос — наполняем кэш списка.
        first = await make_get_request('/api/v1/genres')
        assert first.status == 200
        assert len(first.body) == len(genres_in_es)

        # Удаляем все документы из ES.
        for genre in genres_in_es:
            await es_client.delete(index=es_genres_index, id=genre['id'], refresh=True)

        # Второй запрос — данные из кэша.
        second = await make_get_request('/api/v1/genres')
        assert second.status == 200
        assert len(second.body) == len(genres_in_es)
        assert sorted(second.body, key=lambda x: x['uuid']) == sorted(first.body, key=lambda x: x['uuid'])
