"""Закладки «посмотреть позже»."""

import uuid

from practix_testing.utils.helpers import auth_header


async def test_bookmark_is_created_and_listed(client):
    film_id, headers = uuid.uuid4(), auth_header(user_id=str(uuid.uuid4()))

    response = await client.put(f'/api/v1/bookmarks/{film_id}', headers=headers)
    assert response.status_code == 200
    assert response.json()['film_id'] == str(film_id)

    listed = await client.get('/api/v1/bookmarks', headers=headers)
    assert [item['film_id'] for item in listed.json()] == [str(film_id)]


async def test_bookmark_is_idempotent(client):
    film_id, headers = uuid.uuid4(), auth_header(user_id=str(uuid.uuid4()))

    first = await client.put(f'/api/v1/bookmarks/{film_id}', headers=headers)
    second = await client.put(f'/api/v1/bookmarks/{film_id}', headers=headers)

    assert first.json()['created_at'] == second.json()['created_at'], 'повтор не сдвигает дату добавления'
    listed = await client.get('/api/v1/bookmarks', headers=headers)
    assert len(listed.json()) == 1


async def test_bookmarks_of_other_users_are_invisible(client):
    film_id = uuid.uuid4()
    await client.put(f'/api/v1/bookmarks/{film_id}', headers=auth_header(user_id=str(uuid.uuid4())))

    listed = await client.get('/api/v1/bookmarks', headers=auth_header(user_id=str(uuid.uuid4())))
    assert listed.json() == []


async def test_bookmark_is_deleted_once(client):
    film_id, headers = uuid.uuid4(), auth_header(user_id=str(uuid.uuid4()))
    await client.put(f'/api/v1/bookmarks/{film_id}', headers=headers)

    assert (await client.delete(f'/api/v1/bookmarks/{film_id}', headers=headers)).status_code == 204
    assert (await client.delete(f'/api/v1/bookmarks/{film_id}', headers=headers)).status_code == 404


async def test_bookmarks_require_a_token(client):
    assert (await client.get('/api/v1/bookmarks')).status_code == 401
    assert (await client.put(f'/api/v1/bookmarks/{uuid.uuid4()}')).status_code == 401
