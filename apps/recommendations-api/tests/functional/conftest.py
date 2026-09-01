"""Фикстуры набора: живые recs-db и redis-recs, приложение через ASGI-транспорт.

Схема накатывается МИГРАЦИЕЙ, а не ``create_all``: тогда под тестом оказывается
и сама миграция, включая CHECK-ограничения и предзаполненную строку указателя,
которых в моделях нет. Ровно тот же выбор сделан в наборах UGC и шортенера.

Фикстура миграций синхронная намеренно: ``migrations/env.py`` сам вызывает
``asyncio.run``, и запуск его изнутри работающего цикла дал бы «cannot be called
from a running event loop».
"""

import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from practix_recommendations_api.api.v1 import dependencies
from practix_recommendations_api.core.config import settings
from practix_recommendations_api.db import redis as redis_db
from practix_recommendations_api.db.postgres import get_session
from practix_recommendations_api.main import app
from practix_recommendations_api.services.cache import ShelfCache
from practix_recommendations_api.services.degradation import LastKnownPopular

ALEMBIC_INI = Path(__file__).resolve().parents[2] / 'alembic.ini'
# Списки чистятся TRUNCATE, версии — DELETE. Разница существенная: на
# shelf_version ссылается shelf_pointer, и TRUNCATE ... CASCADE вычистил бы
# заодно и его — вместе с ЕДИНСТВЕННОЙ строкой указателя, которую создаёт
# миграция. Дальше `UPDATE shelf_pointer` обновлял бы ноль строк, витрина
# оставалась бы без указателя, и выдача честно отвечала бы пустым популярным на
# каждый тест. Поэтому же строка указателя восстанавливается явно ниже.
ITEM_TABLES = ('similar_item', 'personal_item', 'popular_item', 'catalog_film')


@pytest.fixture(scope='session', autouse=True)
def migrate() -> None:
    command.upgrade(Config(str(ALEMBIC_INI)), 'head')


@pytest_asyncio.fixture
async def engine():
    # NullPool: движок создаётся на каждый тест, а соединение из чужого цикла
    # дало бы «got Future attached to a different loop».
    created = create_async_engine(settings.database_url, poolclass=pool.NullPool)
    async with created.begin() as conn:
        await conn.execute(text('UPDATE shelf_pointer SET version = NULL WHERE id = true'))
        for table in ITEM_TABLES:
            await conn.execute(text(f'TRUNCATE TABLE {table}'))
        await conn.execute(text('DELETE FROM shelf_version'))
        # Указатель обязан существовать: его создаёт миграция, и его отсутствие
        # неотличимо от «обучение ещё не проходило» — то есть молча ломает набор.
        await conn.execute(
            text(
                'INSERT INTO shelf_pointer (id, version) VALUES (true, NULL) '
                'ON CONFLICT (id) DO UPDATE SET version = NULL'
            )
        )
    yield created
    await created.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def db(session_factory):
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def redis_client():
    client = Redis(
        host=settings.RECS_REDIS_HOST,
        port=settings.RECS_REDIS_PORT,
        db=settings.RECS_REDIS_DB,
        decode_responses=True,
    )
    await client.flushdb()
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def client(session_factory, redis_client):
    """Приложение с подменённой сессией и чистым горячим слоем.

    ``last_known_popular`` пересоздаётся на каждый тест: это память процесса, и
    без сброса популярное из предыдущего теста подменяло бы ступень деградации
    в следующем.
    """

    async def override_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    dependencies.shelf_cache = ShelfCache(redis_client)
    dependencies.last_known_popular = LastKnownPopular()
    # ASGITransport НЕ выполняет lifespan, поэтому модульные клиенты Redis
    # остались бы пустыми. Для горячего слоя это выглядело бы как «кэша нет»
    # (проба готовности честно ответила бы degraded), а для денилиста — куда
    # хуже: политика `deny` трактует отсутствие клиента как «токен отозван», и
    # КАЖДЫЙ запрос с токеном получал бы 401 — то есть персональная выдача не
    # проверялась бы вовсе, а набор выглядел бы упавшим по другой причине.
    redis_db.recs_redis = redis_client
    redis_db.auth_redis = redis_db.create_auth_redis()

    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(
            transport=transport,
            base_url='http://test',
            # RequestIdMiddleware работает в режиме reject_400: без заголовка
            # каждый запрос получил бы 400 ещё до роутера.
            headers={'X-Request-Id': 'test-request-id'},
        ) as http_client:
            yield http_client
    finally:
        app.dependency_overrides.clear()
        dependencies.shelf_cache = None
        if redis_db.auth_redis is not None:
            await redis_db.auth_redis.aclose()
        redis_db.auth_redis = None
        redis_db.recs_redis = None


@pytest.fixture
def film_ids() -> list[uuid.UUID]:
    return [uuid.UUID(int=index) for index in range(1, 11)]


@pytest_asyncio.fixture
async def shelf(db, film_ids):
    """Готовая витрина одной версии: каталог, популярное, соседи, персональное.

    Пишется тем же способом, что и батчем, — через SQL, а не через ORM-модели:
    набор проверяет, что выдача читает витрину в том виде, в каком её
    раскладывает писатель.
    """

    async def build(*, similar=None, personal=None, popular=None, catalog=None) -> int:
        version = (
            await db.execute(
                text(
                    'INSERT INTO shelf_version (run_key, status, models, finished_at) '
                    "VALUES (:key, 'ready', 'cooccurrence,als,popular', now()) RETURNING version"
                ),
                {'key': f'test:{uuid.uuid4()}'},
            )
        ).scalar_one()

        for film_id, rows in (similar or {}).items():
            for rank, (rec_film_id, score) in enumerate(rows):
                await db.execute(
                    text(
                        'INSERT INTO similar_item (version, film_id, rank, rec_film_id, score) '
                        'VALUES (:v, :f, :r, :rf, :s)'
                    ),
                    {'v': version, 'f': film_id, 'r': rank, 'rf': rec_film_id, 's': score},
                )
        for user_id, rows in (personal or {}).items():
            for rank, (film_id, score) in enumerate(rows):
                await db.execute(
                    text(
                        'INSERT INTO personal_item (version, user_id, rank, film_id, score) VALUES (:v, :u, :r, :f, :s)'
                    ),
                    {'v': version, 'u': user_id, 'r': rank, 'f': film_id, 's': score},
                )
        for rank, (film_id, score) in enumerate(popular or []):
            await db.execute(
                text('INSERT INTO popular_item (version, rank, film_id, score) VALUES (:v, :r, :f, :s)'),
                {'v': version, 'r': rank, 'f': film_id, 's': score},
            )
        for film_id in catalog if catalog is not None else film_ids:
            await db.execute(
                text('INSERT INTO catalog_film (version, film_id) VALUES (:v, :f)'),
                {'v': version, 'f': film_id},
            )

        await db.execute(
            text('UPDATE shelf_pointer SET version = :v, switched_at = now() WHERE id = true'),
            {'v': version},
        )
        await db.commit()
        return version

    return build
