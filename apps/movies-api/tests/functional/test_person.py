"""Функциональные тесты для endpoint /api/v1/persons."""

import uuid

import pytest

from practix_testing.utils.helpers import make_film, make_person

pytestmark = pytest.mark.asyncio

# Заранее зафиксированные персоны.
ACTOR = {'id': str(uuid.uuid4()), 'name': 'John Actor'}
DIRECTOR = {'id': str(uuid.uuid4()), 'name': 'Jane Director'}
WRITER = {'id': str(uuid.uuid4()), 'name': 'Bill Writer'}
LONELY = {'id': str(uuid.uuid4()), 'name': 'Solo Star'}  # без фильмов


@pytest.fixture
async def persons_in_es(
    es_persons_index,
    es_movies_index,
    es_write_data,
    flush_redis,
):
    """Готовит набор персон в индексе persons и фильмы, в которых они участвуют."""
    persons = [
        make_person(person_id=ACTOR['id'], full_name=ACTOR['name']),
        make_person(person_id=DIRECTOR['id'], full_name=DIRECTOR['name']),
        make_person(person_id=WRITER['id'], full_name=WRITER['name']),
        make_person(person_id=LONELY['id'], full_name=LONELY['name']),
        make_person(full_name='Random Guy'),
    ]
    films = [
        make_film(
            title='Person Film One',
            imdb_rating=7.5,
            actors=[ACTOR],
            directors=[DIRECTOR],
            writers=[WRITER],
        ),
        make_film(
            title='Person Film Two',
            imdb_rating=8.8,
            actors=[ACTOR],
        ),
    ]
    await es_write_data(persons, es_persons_index)
    await es_write_data(films, es_movies_index)
    return {'persons': persons, 'films': films}


class TestPersonSearch:
    async def test_search_found(self, make_get_request, persons_in_es):
        response = await make_get_request('/api/v1/persons/search', params={'query': 'John'})
        assert response.status == 200
        names = {item['full_name'] for item in response.body}
        assert ACTOR['name'] in names
        # Сериализация ответа.
        for item in response.body:
            assert {'uuid', 'full_name', 'films'} <= set(item.keys())

    async def test_search_actor_has_films_with_roles(
        self,
        make_get_request,
        persons_in_es,
    ):
        response = await make_get_request('/api/v1/persons/search', params={'query': ACTOR['name']})
        assert response.status == 200
        actor = next(p for p in response.body if p['uuid'] == ACTOR['id'])
        assert len(actor['films']) == 2
        for film in actor['films']:
            assert 'actor' in film['roles']

    async def test_search_empty_result(self, make_get_request, persons_in_es):
        response = await make_get_request('/api/v1/persons/search', params={'query': 'NoSuchPerson12345'})
        assert response.status == 200
        assert response.body == []

    async def test_search_required_query(self, make_get_request, persons_in_es):
        response = await make_get_request('/api/v1/persons/search')
        assert response.status == 422

    async def test_search_pagination(self, make_get_request, persons_in_es):
        response = await make_get_request(
            '/api/v1/persons/search',
            params={'query': 'a', 'page_number': 1, 'page_size': 2},
        )
        assert response.status == 200
        assert len(response.body) <= 2

    @pytest.mark.parametrize(
        'params',
        [
            {'query': 'a', 'page_number': 0},
            {'query': 'a', 'page_size': 0},
            {'query': 'a', 'page_size': 1000},
        ],
    )
    async def test_search_validation(self, make_get_request, persons_in_es, params):
        response = await make_get_request('/api/v1/persons/search', params=params)
        assert response.status == 422


class TestPersonsList:
    async def test_list_ok(self, make_get_request, persons_in_es):
        response = await make_get_request('/api/v1/persons')
        assert response.status == 200
        assert isinstance(response.body, list)
        assert len(response.body) == len(persons_in_es['persons'])

    async def test_list_pagination(self, make_get_request, persons_in_es):
        response = await make_get_request('/api/v1/persons', params={'page_size': 2})
        assert response.status == 200
        assert len(response.body) == 2

    async def test_list_cached_in_redis(
        self,
        make_get_request,
        persons_in_es,
        es_client,
        es_persons_index,
    ):
        first = await make_get_request('/api/v1/persons')
        assert first.status == 200

        for person in persons_in_es['persons']:
            await es_client.delete(index=es_persons_index, id=person['id'], refresh=True)

        second = await make_get_request('/api/v1/persons')
        assert second.status == 200
        assert second.body == first.body

    @pytest.mark.parametrize(
        'params',
        [
            {'page_number': 0},
            {'page_size': 0},
            {'page_size': 1000},
        ],
    )
    async def test_list_validation(self, make_get_request, persons_in_es, params):
        response = await make_get_request('/api/v1/persons', params=params)
        assert response.status == 422


class TestPersonDetails:
    async def test_person_by_id_ok(self, make_get_request, persons_in_es):
        response = await make_get_request(f'/api/v1/persons/{ACTOR["id"]}')
        assert response.status == 200
        assert response.body['uuid'] == ACTOR['id']
        assert response.body['full_name'] == ACTOR['name']
        assert len(response.body['films']) == 2
        for film in response.body['films']:
            assert 'actor' in film['roles']

    async def test_person_director_role(self, make_get_request, persons_in_es):
        response = await make_get_request(f'/api/v1/persons/{DIRECTOR["id"]}')
        assert response.status == 200
        assert len(response.body['films']) == 1
        assert response.body['films'][0]['roles'] == ['director']

    async def test_person_writer_role(self, make_get_request, persons_in_es):
        response = await make_get_request(f'/api/v1/persons/{WRITER["id"]}')
        assert response.status == 200
        assert len(response.body['films']) == 1
        assert response.body['films'][0]['roles'] == ['writer']

    async def test_person_without_films(self, make_get_request, persons_in_es):
        response = await make_get_request(f'/api/v1/persons/{LONELY["id"]}')
        assert response.status == 200
        assert response.body['films'] == []

    async def test_person_not_found(self, make_get_request, persons_in_es):
        response = await make_get_request(f'/api/v1/persons/{uuid.uuid4()}')
        assert response.status == 404

    async def test_person_invalid_uuid(self, make_get_request, persons_in_es):
        response = await make_get_request('/api/v1/persons/not-a-uuid')
        assert response.status == 422


class TestPersonFilms:
    async def test_person_films_ok(self, make_get_request, persons_in_es):
        response = await make_get_request(f'/api/v1/persons/{ACTOR["id"]}/film')
        assert response.status == 200
        assert isinstance(response.body, list)
        assert len(response.body) == 2
        titles = {item['title'] for item in response.body}
        assert titles == {'Person Film One', 'Person Film Two'}
        for item in response.body:
            assert {'uuid', 'title', 'imdb_rating'} <= set(item.keys())

    async def test_person_films_empty(self, make_get_request, persons_in_es):
        response = await make_get_request(f'/api/v1/persons/{LONELY["id"]}/film')
        assert response.status == 200
        assert response.body == []

    async def test_person_films_not_found(self, make_get_request, persons_in_es):
        response = await make_get_request(f'/api/v1/persons/{uuid.uuid4()}/film')
        assert response.status == 404

    async def test_person_films_invalid_uuid(self, make_get_request, persons_in_es):
        response = await make_get_request('/api/v1/persons/not-a-uuid/film')
        assert response.status == 422


class TestPersonCache:
    async def test_person_by_id_cached_in_redis(
        self,
        make_get_request,
        persons_in_es,
        es_client,
        es_persons_index,
    ):
        # Первый запрос — наполняет кэш.
        first = await make_get_request(f'/api/v1/persons/{ACTOR["id"]}')
        assert first.status == 200

        # Удаляем персону из ES — если кэш работает, ответ всё ещё придёт.
        await es_client.delete(
            index=es_persons_index,
            id=ACTOR['id'],
            refresh=True,
        )

        second = await make_get_request(f'/api/v1/persons/{ACTOR["id"]}')
        assert second.status == 200
        assert second.body == first.body
