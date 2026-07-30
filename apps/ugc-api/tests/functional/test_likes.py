"""Оценки и преагрегат рейтинга.

Ключевая проверка набора — сценарий W2 исследования: оценка записана и **сразу**
видна в агрегате. Ради этого преагрегат и существует, и ради этого он пишется в
той же транзакции, что и сама оценка.
"""

import asyncio
import uuid

from sqlalchemy import text

from practix_testing.utils.helpers import auth_header


async def rating_of(client, film_id):
    response = await client.get(f'/api/v1/ratings/{film_id}')
    assert response.status_code == 200
    return response.json()


async def test_new_rating_is_visible_in_the_aggregate_immediately(client):
    film_id, user_id = uuid.uuid4(), uuid.uuid4()

    response = await client.put(
        f'/api/v1/likes/{film_id}', json={'rating': 9}, headers=auth_header(user_id=str(user_id))
    )
    assert response.status_code == 200
    body = response.json()
    assert body['rating'] == 9
    assert body['film_rating']['ratings_count'] == 1

    aggregate = await rating_of(client, film_id)
    assert aggregate['ratings_count'] == 1
    assert aggregate['average'] == 9.0
    assert aggregate['likes'] == 1
    assert aggregate['dislikes'] == 0
    assert aggregate['histogram'][9] == 1
    assert sum(aggregate['histogram']) == 1


async def test_changed_rating_does_not_inflate_the_count(client):
    film_id, headers = uuid.uuid4(), auth_header()

    await client.put(f'/api/v1/likes/{film_id}', json={'rating': 3}, headers=headers)
    await client.put(f'/api/v1/likes/{film_id}', json={'rating': 8}, headers=headers)

    aggregate = await rating_of(client, film_id)
    assert aggregate['ratings_count'] == 1, 'пользователь один — оценка одна'
    assert aggregate['average'] == 8.0
    assert aggregate['histogram'][3] == 0
    assert aggregate['histogram'][8] == 1


async def test_repeating_the_same_rating_changes_nothing(client):
    film_id, headers = uuid.uuid4(), auth_header()

    await client.put(f'/api/v1/likes/{film_id}', json={'rating': 7}, headers=headers)
    before = await rating_of(client, film_id)
    await client.put(f'/api/v1/likes/{film_id}', json={'rating': 7}, headers=headers)
    after = await rating_of(client, film_id)

    assert before == after


async def test_dislike_is_just_a_low_rating(client):
    film_id = uuid.uuid4()
    await client.put(f'/api/v1/likes/{film_id}', json={'rating': 0}, headers=auth_header())

    aggregate = await rating_of(client, film_id)
    assert aggregate['likes'] == 0
    assert aggregate['dislikes'] == 1
    assert aggregate['histogram'][0] == 1


async def test_threshold_is_reported_and_splits_the_histogram(client):
    film_id = uuid.uuid4()
    for rating in (2, 5, 9, 10):
        await client.put(f'/api/v1/likes/{film_id}', json={'rating': rating}, headers=auth_header())

    aggregate = await rating_of(client, film_id)
    threshold = aggregate['like_threshold']
    assert aggregate['likes'] == sum(aggregate['histogram'][threshold:])
    assert aggregate['dislikes'] == sum(aggregate['histogram'][:threshold])
    assert aggregate['likes'] + aggregate['dislikes'] == aggregate['ratings_count']


async def test_deleting_a_rating_returns_the_aggregate_to_zero(client):
    film_id, headers = uuid.uuid4(), auth_header()
    await client.put(f'/api/v1/likes/{film_id}', json={'rating': 6}, headers=headers)

    assert (await client.delete(f'/api/v1/likes/{film_id}', headers=headers)).status_code == 204

    aggregate = await rating_of(client, film_id)
    assert aggregate['ratings_count'] == 0
    assert aggregate['average'] is None
    assert sum(aggregate['histogram']) == 0


async def test_deleting_a_rating_that_was_never_set_is_404(client):
    response = await client.delete(f'/api/v1/likes/{uuid.uuid4()}', headers=auth_header())
    assert response.status_code == 404


async def test_unrated_film_returns_zeros_not_404(client):
    aggregate = await rating_of(client, uuid.uuid4())
    assert aggregate['ratings_count'] == 0
    assert aggregate['average'] is None


async def test_writing_without_a_token_is_401(client):
    response = await client.put(f'/api/v1/likes/{uuid.uuid4()}', json={'rating': 5})
    assert response.status_code == 401


async def test_liked_films_are_sorted_and_filtered(client):
    user_id = str(uuid.uuid4())
    headers = auth_header(user_id=user_id)
    films = {rating: uuid.uuid4() for rating in (3, 8, 10)}
    for rating, film_id in films.items():
        await client.put(f'/api/v1/likes/{film_id}', json={'rating': rating}, headers=headers)

    response = await client.get('/api/v1/likes/me', headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert [item['rating'] for item in body] == [10, 8], 'оценка 3 ниже порога «понравилось»'

    response = await client.get('/api/v1/likes/me?min_rating=0', headers=headers)
    assert [item['rating'] for item in response.json()] == [10, 8, 3]


async def test_liked_films_are_paginated(client):
    headers = auth_header(user_id=str(uuid.uuid4()))
    for rating in (8, 9, 10):
        await client.put(f'/api/v1/likes/{uuid.uuid4()}', json={'rating': rating}, headers=headers)

    first = await client.get('/api/v1/likes/me?page_size=2&page_number=1', headers=headers)
    second = await client.get('/api/v1/likes/me?page_size=2&page_number=2', headers=headers)
    assert [item['rating'] for item in first.json()] == [10, 9]
    assert [item['rating'] for item in second.json()] == [8]


async def test_page_size_over_the_limit_is_rejected(client):
    response = await client.get('/api/v1/likes/me?page_size=1000', headers=auth_header())
    assert response.status_code == 422


async def test_concurrent_first_ratings_do_not_double_count(client, db):
    """Два одновременных запроса на ПЕРВУЮ оценку одной пары (пользователь, фильм).

    Без блокировки обе транзакции видят «прошлой оценки нет» и обе прибавляют
    +1 к количеству: у одного пользователя оказывается две оценки, и сумма
    гистограммы перестаёт сходиться со счётчиком. Блокировкой строки это не
    лечится — строки ещё нет.
    """
    film_id, headers = uuid.uuid4(), auth_header(user_id=str(uuid.uuid4()))

    await asyncio.gather(
        client.put(f'/api/v1/likes/{film_id}', json={'rating': 7}, headers=headers),
        client.put(f'/api/v1/likes/{film_id}', json={'rating': 9}, headers=headers),
    )

    aggregate = await rating_of(client, film_id)
    assert aggregate['ratings_count'] == 1
    assert sum(aggregate['histogram']) == 1
    assert aggregate['average'] == aggregate['histogram'].index(1)


async def test_concurrent_updates_keep_the_aggregate_consistent(client, db):
    """Два одновременных ИЗМЕНЕНИЯ уже существующей оценки.

    Здесь ломается иначе: подзапрос «прошлая оценка» читает снимок начала
    оператора, а сам upsert меняет уже новую версию строки, — вторая транзакция
    посчитала бы дельту от устаревшего значения.
    """
    film_id, headers = uuid.uuid4(), auth_header(user_id=str(uuid.uuid4()))
    await client.put(f'/api/v1/likes/{film_id}', json={'rating': 5}, headers=headers)

    await asyncio.gather(
        client.put(f'/api/v1/likes/{film_id}', json={'rating': 8}, headers=headers),
        client.put(f'/api/v1/likes/{film_id}', json={'rating': 3}, headers=headers),
    )

    stored = await db.scalar(text('SELECT rating FROM likes WHERE film_id = :film_id'), {'film_id': film_id})
    aggregate = await rating_of(client, film_id)
    assert aggregate['ratings_count'] == 1
    assert aggregate['average'] == stored, 'преагрегат обязан совпадать с фактической оценкой'
    assert aggregate['histogram'][stored] == 1
    assert sum(aggregate['histogram']) == 1
