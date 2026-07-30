"""Функциональные тесты для endpoint /api/v1/films."""

import uuid

import pytest

from practix_testing.utils.helpers import make_film

pytestmark = pytest.mark.asyncio

GENRE_ACTION = {'id': str(uuid.uuid4()), 'name': 'Action'}
GENRE_DRAMA = {'id': str(uuid.uuid4()), 'name': 'Drama'}


@pytest.fixture
async def films_in_es(es_movies_index, es_write_data, flush_redis):
    """Загружает заранее подготовленный набор фильмов в ES."""
    docs = [
        make_film(title='The Star', imdb_rating=8.5, genres=[GENRE_ACTION]),
        make_film(title='Star Wars', imdb_rating=9.1, genres=[GENRE_ACTION]),
        make_film(title='Mashed potato', imdb_rating=3.2, genres=[GENRE_DRAMA]),
        make_film(title='Drama queen', imdb_rating=7.0, genres=[GENRE_DRAMA]),
        make_film(title='Random film', imdb_rating=5.5, genres=[GENRE_DRAMA]),
    ]
    await es_write_data(docs, es_movies_index)
    return docs


class TestFilmsList:
    async def test_list_ok(self, make_get_request, films_in_es):
        response = await make_get_request('/api/v1/films')
        assert response.status == 200
        assert isinstance(response.body, list)
        assert len(response.body) == len(films_in_es)
        for item in response.body:
            assert {'uuid', 'title', 'imdb_rating'} <= set(item.keys())

    async def test_list_pagination(self, make_get_request, films_in_es):
        response = await make_get_request('/api/v1/films', params={'page_number': 1, 'page_size': 2})
        assert response.status == 200
        assert len(response.body) == 2

    async def test_list_sort_desc(self, make_get_request, films_in_es):
        response = await make_get_request('/api/v1/films', params={'sort': '-imdb_rating'})
        assert response.status == 200
        ratings = [item['imdb_rating'] for item in response.body]
        assert ratings == sorted(ratings, reverse=True)

    async def test_list_sort_asc(self, make_get_request, films_in_es):
        response = await make_get_request('/api/v1/films', params={'sort': 'imdb_rating'})
        assert response.status == 200
        ratings = [item['imdb_rating'] for item in response.body]
        assert ratings == sorted(ratings)

    async def test_list_filter_by_genre(self, make_get_request, films_in_es):
        response = await make_get_request('/api/v1/films', params={'genre': GENRE_ACTION['id']})
        assert response.status == 200
        titles = {item['title'] for item in response.body}
        assert titles == {'The Star', 'Star Wars'}

    @pytest.mark.parametrize(
        'params',
        [
            {'page_number': 0},
            {'page_size': 0},
            {'page_size': 1000},
            {'genre': 'not-a-uuid'},
        ],
    )
    async def test_list_validation(self, make_get_request, params):
        response = await make_get_request('/api/v1/films', params=params)
        assert response.status == 422


class TestFilmSearch:
    async def test_search_found(self, make_get_request, films_in_es):
        response = await make_get_request('/api/v1/films/search', params={'query': 'Star'})
        assert response.status == 200
        titles = {item['title'] for item in response.body}
        assert {'The Star', 'Star Wars'} <= titles

    async def test_search_empty_result(self, make_get_request, films_in_es):
        response = await make_get_request('/api/v1/films/search', params={'query': 'NoSuchFilmExists12345'})
        assert response.status == 200
        assert response.body == []

    async def test_search_required_query(self, make_get_request, films_in_es):
        response = await make_get_request('/api/v1/films/search')
        assert response.status == 422

    async def test_search_pagination(self, make_get_request, films_in_es):
        response = await make_get_request(
            '/api/v1/films/search',
            params={'query': 'Star', 'page_number': 1, 'page_size': 1},
        )
        assert response.status == 200
        assert len(response.body) == 1


class TestFilmDetails:
    async def test_film_by_id_ok(self, make_get_request, films_in_es):
        target = films_in_es[0]
        response = await make_get_request(f'/api/v1/films/{target["id"]}')
        assert response.status == 200
        assert response.body['uuid'] == target['id']
        assert response.body['title'] == target['title']
        assert response.body['imdb_rating'] == target['imdb_rating']
        assert {'description', 'genre', 'actors', 'writers', 'directors'} <= set(response.body)
        assert len(response.body['genre']) == len(target['genres'])

    async def test_film_not_found(self, make_get_request, films_in_es):
        response = await make_get_request(f'/api/v1/films/{uuid.uuid4()}')
        assert response.status == 404

    async def test_film_invalid_uuid(self, make_get_request, films_in_es):
        response = await make_get_request('/api/v1/films/not-a-uuid')
        assert response.status == 422


class TestFilmCache:
    async def test_film_by_id_cached_in_redis(
        self,
        make_get_request,
        films_in_es,
        redis_client,
        es_client,
        es_movies_index,
    ):
        target = films_in_es[0]
        # Первый запрос — наполняем кэш.
        first = await make_get_request(f'/api/v1/films/{target["id"]}')
        assert first.status == 200

        # Удаляем документ из ES — если кэш работает, ответ всё ещё придёт.
        await es_client.delete(index=es_movies_index, id=target['id'], refresh=True)

        second = await make_get_request(f'/api/v1/films/{target["id"]}')
        assert second.status == 200
        assert second.body == first.body
