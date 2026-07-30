"""Функциональные тесты для endpoint /search (фильмы и персоны)."""

import uuid

import pytest

from practix_testing.utils.helpers import make_film, make_person

pytestmark = pytest.mark.asyncio

GENRE_SCIFI = {'id': str(uuid.uuid4()), 'name': 'Sci-Fi'}
GENRE_HORROR = {'id': str(uuid.uuid4()), 'name': 'Horror'}


@pytest.fixture
async def search_films_in_es(es_movies_index, es_write_data, flush_redis):
    """Загружает фильмы для тестирования поиска."""
    docs = [
        make_film(
            title='The Matrix',
            imdb_rating=8.7,
            genres=[GENRE_SCIFI],
            description='A computer hacker learns about the true nature of reality',
        ),
        make_film(
            title='Matrix Reloaded',
            imdb_rating=7.2,
            genres=[GENRE_SCIFI],
            description='Neo and the rebel leaders continue the fight',
        ),
        make_film(
            title='Matrix Revolutions',
            imdb_rating=6.7,
            genres=[GENRE_SCIFI],
            description='The human city of Zion defends itself',
        ),
        make_film(
            title='Alien',
            imdb_rating=8.5,
            genres=[GENRE_HORROR],
            description='The crew of a commercial spacecraft encounter a deadly alien',
        ),
        make_film(
            title='Aliens',
            imdb_rating=8.4,
            genres=[GENRE_HORROR],
            description='Ripley returns to the planet where her crew encountered the alien',
        ),
        make_film(
            title='Inception',
            imdb_rating=8.8,
            genres=[GENRE_SCIFI],
            description='A thief who steals secrets through dream-sharing technology',
        ),
        make_film(
            title='Interstellar',
            imdb_rating=8.6,
            genres=[GENRE_SCIFI],
            description='A team of explorers travel through a wormhole in space',
        ),
    ]
    await es_write_data(docs, es_movies_index)
    return docs


@pytest.fixture
async def search_persons_in_es(es_persons_index, es_movies_index, es_write_data, flush_redis):
    """Загружает персон и фильмы для тестирования поиска персон."""
    persons = [
        make_person(full_name='Keanu Reeves'),
        make_person(full_name='Carrie-Anne Moss'),
        make_person(full_name='Laurence Fishburne'),
        make_person(full_name='Christopher Nolan'),
        make_person(full_name='Sigourney Weaver'),
    ]
    actor_keanu = {'id': persons[0]['id'], 'name': 'Keanu Reeves'}
    actor_carrie = {'id': persons[1]['id'], 'name': 'Carrie-Anne Moss'}
    director_nolan = {'id': persons[3]['id'], 'name': 'Christopher Nolan'}
    films = [
        make_film(title='The Matrix', imdb_rating=8.7, actors=[actor_keanu, actor_carrie], directors=[], writers=[]),
        make_film(title='Inception', imdb_rating=8.8, actors=[actor_keanu], directors=[director_nolan], writers=[]),
    ]
    await es_write_data(persons, es_persons_index)
    await es_write_data(films, es_movies_index)
    return {'persons': persons, 'films': films}


class TestFilmSearch:
    """Тесты полнотекстового поиска фильмов /api/v1/films/search."""

    async def test_search_by_title(self, make_get_request, search_films_in_es):
        response = await make_get_request('/api/v1/films/search', params={'query': 'Matrix'})
        assert response.status == 200
        titles = {item['title'] for item in response.body}
        assert 'The Matrix' in titles
        assert 'Matrix Reloaded' in titles
        assert 'Matrix Revolutions' in titles

    async def test_search_by_description(self, make_get_request, search_films_in_es):
        response = await make_get_request('/api/v1/films/search', params={'query': 'wormhole space'})
        assert response.status == 200
        titles = {item['title'] for item in response.body}
        assert 'Interstellar' in titles

    async def test_search_empty_result(self, make_get_request, search_films_in_es):
        response = await make_get_request('/api/v1/films/search', params={'query': 'NonExistentFilm99999'})
        assert response.status == 200
        assert response.body == []

    async def test_search_required_query(self, make_get_request, search_films_in_es):
        response = await make_get_request('/api/v1/films/search')
        assert response.status == 422

    async def test_search_pagination_page_size(self, make_get_request, search_films_in_es):
        response = await make_get_request('/api/v1/films/search', params={'query': 'Matrix', 'page_size': 1})
        assert response.status == 200
        assert len(response.body) == 1

    async def test_search_pagination_second_page(self, make_get_request, search_films_in_es):
        first_page = await make_get_request(
            '/api/v1/films/search', params={'query': 'Matrix', 'page_number': 1, 'page_size': 2}
        )
        second_page = await make_get_request(
            '/api/v1/films/search', params={'query': 'Matrix', 'page_number': 2, 'page_size': 2}
        )
        assert first_page.status == 200
        assert second_page.status == 200
        first_ids = {item['uuid'] for item in first_page.body}
        second_ids = {item['uuid'] for item in second_page.body}
        assert first_ids.isdisjoint(second_ids)

    async def test_search_response_schema(self, make_get_request, search_films_in_es):
        response = await make_get_request('/api/v1/films/search', params={'query': 'Alien'})
        assert response.status == 200
        for item in response.body:
            assert {'uuid', 'title', 'imdb_rating'} <= set(item.keys())

    @pytest.mark.parametrize(
        'params',
        [
            {'query': 'Matrix', 'page_number': 0},
            {'query': 'Matrix', 'page_size': 0},
            {'query': 'Matrix', 'page_size': 1000},
        ],
    )
    async def test_search_validation(self, make_get_request, search_films_in_es, params):
        response = await make_get_request('/api/v1/films/search', params=params)
        assert response.status == 422

    async def test_search_cached_in_redis(
        self,
        make_get_request,
        search_films_in_es,
        es_client,
        es_movies_index,
    ):
        # Первый запрос — наполняем кэш.
        first = await make_get_request('/api/v1/films/search', params={'query': 'Inception'})
        assert first.status == 200
        assert len(first.body) >= 1

        # Удаляем все документы из ES.
        for film in search_films_in_es:
            await es_client.delete(index=es_movies_index, id=film['id'], refresh=True)

        # Второй запрос — данные из кэша.
        second = await make_get_request('/api/v1/films/search', params={'query': 'Inception'})
        assert second.status == 200
        assert second.body == first.body


class TestPersonSearch:
    """Тесты полнотекстового поиска персон /api/v1/persons/search."""

    async def test_search_by_name(self, make_get_request, search_persons_in_es):
        response = await make_get_request('/api/v1/persons/search', params={'query': 'Keanu'})
        assert response.status == 200
        names = {item['full_name'] for item in response.body}
        assert 'Keanu Reeves' in names

    async def test_search_person_has_films(self, make_get_request, search_persons_in_es):
        response = await make_get_request('/api/v1/persons/search', params={'query': 'Keanu Reeves'})
        assert response.status == 200
        keanu = next(p for p in response.body if p['full_name'] == 'Keanu Reeves')
        assert len(keanu['films']) == 2
        for film in keanu['films']:
            assert 'actor' in film['roles']

    async def test_search_person_director_role(self, make_get_request, search_persons_in_es):
        response = await make_get_request('/api/v1/persons/search', params={'query': 'Christopher Nolan'})
        assert response.status == 200
        nolan = next(p for p in response.body if p['full_name'] == 'Christopher Nolan')
        assert len(nolan['films']) == 1
        assert 'director' in nolan['films'][0]['roles']

    async def test_search_empty_result(self, make_get_request, search_persons_in_es):
        response = await make_get_request('/api/v1/persons/search', params={'query': 'NoSuchPerson99999'})
        assert response.status == 200
        assert response.body == []

    async def test_search_required_query(self, make_get_request, search_persons_in_es):
        response = await make_get_request('/api/v1/persons/search')
        assert response.status == 422

    async def test_search_pagination(self, make_get_request, search_persons_in_es):
        response = await make_get_request(
            '/api/v1/persons/search', params={'query': 'a', 'page_number': 1, 'page_size': 2}
        )
        assert response.status == 200
        assert len(response.body) <= 2

    async def test_search_response_schema(self, make_get_request, search_persons_in_es):
        response = await make_get_request('/api/v1/persons/search', params={'query': 'Keanu'})
        assert response.status == 200
        for item in response.body:
            assert {'uuid', 'full_name', 'films'} <= set(item.keys())
            for film in item['films']:
                assert {'uuid', 'roles'} <= set(film.keys())

    @pytest.mark.parametrize(
        'params',
        [
            {'query': 'a', 'page_number': 0},
            {'query': 'a', 'page_size': 0},
            {'query': 'a', 'page_size': 1000},
        ],
    )
    async def test_search_validation(self, make_get_request, search_persons_in_es, params):
        response = await make_get_request('/api/v1/persons/search', params=params)
        assert response.status == 422

    async def test_search_cached_in_redis(
        self,
        make_get_request,
        search_persons_in_es,
        es_client,
        es_persons_index,
    ):
        # Первый запрос — наполняем кэш.
        first = await make_get_request('/api/v1/persons/search', params={'query': 'Keanu'})
        assert first.status == 200
        assert len(first.body) >= 1

        # Удаляем все персон из ES.
        for person in search_persons_in_es['persons']:
            await es_client.delete(index=es_persons_index, id=person['id'], refresh=True)

        # Второй запрос — данные из кэша.
        second = await make_get_request('/api/v1/persons/search', params={'query': 'Keanu'})
        assert second.status == 200
        assert second.body == first.body
