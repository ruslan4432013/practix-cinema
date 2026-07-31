"""Служебное: заголовки, пробы, отозванные токены и обслуживание преагрегата."""

import uuid

import jwt
from sqlalchemy import text

import practix_ugc_api.db.redis as redis_module
from practix_testing.utils.helpers import JWT_ALGORITHM, JWT_SECRET, auth_header, make_access_token
from practix_ugc_api.cli import _DIVERGENCE, _DROP_ORPHANS, _RECOUNT
from practix_ugc_api.db.redis import get_auth_redis
from practix_ugc_api.main import app


async def test_request_without_request_id_is_rejected(client):
    """Заголовок обязателен: в проде его ставит Nginx, и его отсутствие — сигнал обхода."""
    response = await client.get(f'/api/v1/ratings/{uuid.uuid4()}', headers={'X-Request-Id': ''})
    assert response.status_code == 400


async def test_liveness_and_readiness(client):
    assert (await client.get('/health/live')).status_code == 200

    ready = await client.get('/health/ready')
    assert ready.status_code == 200
    assert ready.json() == {'status': 'ok', 'database_connected': True, 'auth_denylist_connected': True}


async def test_readiness_reports_denylist_outage_while_public_reads_keep_working(client):
    """Отказ Redis денилиста виден в пробе, но не выводит сервис из ротации.

    Отсутствие клиента — та же ветка, что и ошибка Redis: при `on_error='deny'`
    загрузчик денилиста в обоих случаях считает токен отозванным. Подменяется и
    зависимость пробы, и модульный синглтон — загрузчик ходит к нему напрямую,
    мимо `dependency_overrides`.
    """
    redis_module.auth_redis = None
    app.dependency_overrides[get_auth_redis] = lambda: None
    try:
        ready = await client.get('/health/ready')
        assert ready.status_code == 200, 'экземпляр Redis общий на все реплики — 503 отключил бы и чтение'
        assert ready.json() == {'status': 'degraded', 'database_connected': True, 'auth_denylist_connected': False}

        # Ровно то, о чём сообщает `degraded`: записи отклоняются, чтения живы.
        film_id = uuid.uuid4()
        write = await client.put(f'/api/v1/likes/{film_id}', json={'rating': 9}, headers=auth_header())
        assert write.status_code == 401
        assert (await client.get(f'/api/v1/ratings/{film_id}')).status_code == 200
    finally:
        del app.dependency_overrides[get_auth_redis]


async def test_revoked_token_is_rejected_and_writes_nothing(client, auth_redis, db):
    token = make_access_token()
    jti = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])['jti']
    await auth_redis.set(jti, 'revoked')

    film_id = uuid.uuid4()
    response = await client.put(
        f'/api/v1/likes/{film_id}', json={'rating': 9}, headers={'Authorization': f'Bearer {token}'}
    )

    assert response.status_code == 401
    written = await db.scalar(text('SELECT count(*) FROM likes WHERE film_id = :film_id'), {'film_id': film_id})
    assert written == 0


async def test_expired_token_is_rejected(client, db):
    """Просроченный токен не даёт записать.

    Код ответа проверяется как «любой отказ», и это фиксация факта, а не
    небрежность: `async_fastapi_jwt_auth` отдаёт на просроченный токен
    `JWTDecodeError` со статусом **422**, а не 401. Статус берётся из самой
    библиотеки общим обработчиком `practix_core.jwt.install_exception_handler`,
    то есть это контракт, разделяемый с Auth и коллектором. Переопределять его в
    одном сервисе значило бы развести три сервиса по кодам ответа ради одной
    ручки; см. «не сделано» в README сервиса.
    """
    film_id = uuid.uuid4()
    headers = {'Authorization': f'Bearer {make_access_token(expires_in=-10)}'}
    response = await client.put(f'/api/v1/likes/{film_id}', json={'rating': 9}, headers=headers)

    assert response.status_code in (401, 422)
    written = await db.scalar(text('SELECT count(*) FROM likes WHERE film_id = :film_id'), {'film_id': film_id})
    assert written == 0


async def test_token_with_a_non_uuid_subject_is_rejected(client):
    headers = auth_header(user_id='не-uuid')
    response = await client.put(f'/api/v1/likes/{uuid.uuid4()}', json={'rating': 9}, headers=headers)
    assert response.status_code == 401


async def test_public_reads_need_no_token(client):
    assert (await client.get(f'/api/v1/ratings/{uuid.uuid4()}')).status_code == 200
    assert (await client.get(f'/api/v1/reviews?film_id={uuid.uuid4()}')).status_code == 200


async def test_recount_detects_and_repairs_drift(client, db):
    """Расхождение преагрегата находится и чинится.

    Подкладываем расхождение вручную — именно так оно и появляется в жизни:
    правкой в обход сервиса или восстановлением не того бэкапа.
    """
    film_id = uuid.uuid4()
    await client.put(f'/api/v1/likes/{film_id}', json={'rating': 9}, headers=auth_header())

    assert await db.scalar(_DIVERGENCE) == 0

    await db.execute(
        text('UPDATE film_rating SET ratings_count = 42, ratings_sum = 400 WHERE film_id = :film_id'),
        {'film_id': film_id},
    )
    await db.commit()
    assert await db.scalar(_DIVERGENCE) == 1

    await db.execute(_RECOUNT)
    await db.execute(_DROP_ORPHANS)
    await db.commit()
    assert await db.scalar(_DIVERGENCE) == 0


async def test_recount_drops_aggregates_of_films_without_ratings(client, db):
    film_id, headers = uuid.uuid4(), auth_header()
    await client.put(f'/api/v1/likes/{film_id}', json={'rating': 4}, headers=headers)
    await client.delete(f'/api/v1/likes/{film_id}', headers=headers)

    await db.execute(_DROP_ORPHANS)
    await db.commit()

    remaining = await db.scalar(text('SELECT count(*) FROM film_rating WHERE film_id = :film_id'), {'film_id': film_id})
    assert remaining == 0
    assert await db.scalar(_DIVERGENCE) == 0
