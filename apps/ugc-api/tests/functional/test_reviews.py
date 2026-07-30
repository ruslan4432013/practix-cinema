"""Рецензии, гибкая сортировка и голосование за полезность."""

import uuid

from sqlalchemy import text

from practix_testing.utils.helpers import auth_header


async def create_review(client, film_id, *, user_id=None, body='Хорошее кино', author_rating=None):
    headers = auth_header(user_id=user_id or str(uuid.uuid4()))
    response = await client.post(
        '/api/v1/reviews',
        json={'film_id': str(film_id), 'body': body, 'author_rating': author_rating},
        headers=headers,
    )
    return response, headers


async def test_review_is_created_and_readable(client):
    film_id = uuid.uuid4()
    response, _ = await create_review(client, film_id, body='Отличный фильм', author_rating=9)
    assert response.status_code == 201
    review = response.json()
    assert review['body'] == 'Отличный фильм'
    assert review['useful_score'] == 0

    fetched = await client.get(f'/api/v1/reviews/{review["review_id"]}')
    assert fetched.status_code == 200
    assert fetched.json()['body'] == 'Отличный фильм'


async def test_second_review_of_the_same_film_is_rejected(client):
    film_id, user_id = uuid.uuid4(), str(uuid.uuid4())
    first, _ = await create_review(client, film_id, user_id=user_id)
    second, _ = await create_review(client, film_id, user_id=user_id)

    assert first.status_code == 201
    assert second.status_code == 409


async def test_review_list_does_not_leak_the_body(client):
    film_id = uuid.uuid4()
    await create_review(client, film_id)

    listed = await client.get(f'/api/v1/reviews?film_id={film_id}')
    assert listed.status_code == 200
    assert 'body' not in listed.json()[0]


async def test_three_sort_orders_give_three_orders(client):
    film_id = uuid.uuid4()
    ids = {}
    for label, author_rating in (('первая', 2), ('вторая', 10), ('третья', 5)):
        response, _ = await create_review(client, film_id, body=label, author_rating=author_rating)
        ids[label] = response.json()['review_id']

    # Полезность задаём голосами: третья рецензия полезнее всех.
    for _ in range(3):
        await client.put(
            f'/api/v1/reviews/{ids["третья"]}/vote', json={'value': 1}, headers=auth_header(user_id=str(uuid.uuid4()))
        )

    by_new = await client.get(f'/api/v1/reviews?film_id={film_id}&sort=new')
    by_useful = await client.get(f'/api/v1/reviews?film_id={film_id}&sort=useful')
    by_rating = await client.get(f'/api/v1/reviews?film_id={film_id}&sort=rating')

    assert by_useful.json()[0]['review_id'] == ids['третья']
    assert by_rating.json()[0]['review_id'] == ids['вторая']
    assert {item['review_id'] for item in by_new.json()} == set(ids.values())


async def test_reviews_without_author_rating_do_not_top_the_rating_sort(client):
    film_id = uuid.uuid4()
    await create_review(client, film_id, body='без оценки', author_rating=None)
    rated, _ = await create_review(client, film_id, body='с оценкой', author_rating=7)

    by_rating = await client.get(f'/api/v1/reviews?film_id={film_id}&sort=rating')
    assert by_rating.json()[0]['review_id'] == rated.json()['review_id']


async def test_unknown_sort_is_rejected(client):
    response = await client.get(f'/api/v1/reviews?film_id={uuid.uuid4()}&sort=created_at')
    assert response.status_code == 422


async def test_only_the_author_or_a_moderator_deletes_a_review(client):
    film_id = uuid.uuid4()
    author_id = str(uuid.uuid4())
    response, author_headers = await create_review(client, film_id, user_id=author_id)
    review_id = response.json()['review_id']

    stranger = await client.delete(f'/api/v1/reviews/{review_id}', headers=auth_header(user_id=str(uuid.uuid4())))
    assert stranger.status_code == 403

    moderator = await client.delete(f'/api/v1/reviews/{review_id}', headers=auth_header(roles=['admin']))
    assert moderator.status_code == 204

    assert (await client.delete(f'/api/v1/reviews/{review_id}', headers=author_headers)).status_code == 404


async def test_deleting_a_review_removes_its_votes(client, db):
    film_id = uuid.uuid4()
    response, author_headers = await create_review(client, film_id)
    review_id = response.json()['review_id']
    await client.put(f'/api/v1/reviews/{review_id}/vote', json={'value': 1}, headers=auth_header())

    await client.delete(f'/api/v1/reviews/{review_id}', headers=author_headers)

    # Внешних ключей в схеме нет — уборка голосов обязана быть явной.
    orphans = await db.scalar(
        text('SELECT count(*) FROM review_votes WHERE review_id = :review_id'), {'review_id': review_id}
    )
    assert orphans == 0


async def test_votes_move_the_counters(client):
    response, _ = await create_review(client, uuid.uuid4())
    review_id = response.json()['review_id']
    voter = auth_header(user_id=str(uuid.uuid4()))

    up = await client.put(f'/api/v1/reviews/{review_id}/vote', json={'value': 1}, headers=voter)
    assert up.json() == {'review_id': review_id, 'votes_likes': 1, 'votes_dislikes': 0, 'useful_score': 1}

    # Переголосование, а не второй голос: счётчики переезжают, а не растут.
    down = await client.put(f'/api/v1/reviews/{review_id}/vote', json={'value': -1}, headers=voter)
    assert down.json() == {'review_id': review_id, 'votes_likes': 0, 'votes_dislikes': 1, 'useful_score': -1}

    repeat = await client.put(f'/api/v1/reviews/{review_id}/vote', json={'value': -1}, headers=voter)
    assert repeat.json() == down.json()


async def test_retracting_a_vote_returns_the_counters_to_zero(client):
    response, _ = await create_review(client, uuid.uuid4())
    review_id = response.json()['review_id']
    voter = auth_header(user_id=str(uuid.uuid4()))
    await client.put(f'/api/v1/reviews/{review_id}/vote', json={'value': 1}, headers=voter)

    assert (await client.delete(f'/api/v1/reviews/{review_id}/vote', headers=voter)).status_code == 204

    fetched = await client.get(f'/api/v1/reviews/{review_id}')
    assert fetched.json()['useful_score'] == 0
    assert fetched.json()['votes_likes'] == 0


async def test_vote_for_a_missing_review_leaves_no_orphan(client, db):
    """Роль отсутствующего ON DELETE CASCADE играет откат транзакции."""
    review_id = uuid.uuid4()
    response = await client.put(f'/api/v1/reviews/{review_id}/vote', json={'value': 1}, headers=auth_header())
    assert response.status_code == 404

    orphans = await db.scalar(
        text('SELECT count(*) FROM review_votes WHERE review_id = :review_id'), {'review_id': review_id}
    )
    assert orphans == 0


async def test_review_writes_require_a_token(client):
    response = await client.post('/api/v1/reviews', json={'film_id': str(uuid.uuid4()), 'body': 'текст'})
    assert response.status_code == 401


async def test_smuggled_user_id_is_rejected(client):
    response = await client.post(
        '/api/v1/reviews',
        json={'film_id': str(uuid.uuid4()), 'body': 'текст', 'user_id': str(uuid.uuid4())},
        headers=auth_header(),
    )
    assert response.status_code == 422, 'user_id берётся только из токена и в теле недопустим'
