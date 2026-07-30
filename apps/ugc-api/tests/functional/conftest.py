"""Фикстуры функционального набора.

Схема разворачивается **миграцией**, а не ``Base.metadata.create_all``, — и это
отличие от набора Auth осознанное. Схема здесь описана ручным DDL (значение по
умолчанию-выражение, CHECK-ограничения, DESC-индексы), поэтому ``create_all``
собрал бы схему из моделей — то есть НЕ ту, на которой сервис работает в
продакшене. Заодно миграция оказывается под тестом на каждом прогоне.

Между тестами таблицы очищаются TRUNCATE, а не пересоздаются: так быстрее и так
проверяется ровно та схема, которую накатила миграция.
"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import practix_ugc_api.db.redis as redis_module
from practix_ugc_api.core.config import settings
from practix_ugc_api.db.postgres import get_session
from practix_ugc_api.main import app

ALEMBIC_INI = Path(__file__).resolve().parents[2] / 'alembic.ini'
TABLES = 'likes, film_rating, bookmarks, reviews, review_votes'


@pytest.fixture(scope='session', autouse=True)
def migrate() -> None:
    """Накатывает миграции один раз на прогон.

    Фикстура намеренно синхронная: env.py сам вызывает ``asyncio.run``, и внутри
    уже работающего цикла это упало бы с «asyncio.run() cannot be called from a
    running event loop».
    """
    command.upgrade(Config(str(ALEMBIC_INI)), 'head')


@pytest.fixture
async def engine():
    engine = create_async_engine(settings.database_url, poolclass=pool.NullPool)
    async with engine.begin() as conn:
        await conn.execute(text(f'TRUNCATE {TABLES}'))
    yield engine
    await engine.dispose()


@pytest.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
async def db(session_factory):
    """Прямой доступ к базе — для проверок, которых нет в HTTP-контракте."""
    async with session_factory() as session:
        yield session


@pytest.fixture
async def auth_redis():
    """Redis Auth-сервиса: сервис читает оттуда денилист отозванных токенов."""
    client = Redis(
        host=settings.auth_redis_host,
        port=settings.auth_redis_port,
        db=settings.AUTH_REDIS_DB,
        decode_responses=True,
    )
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.fixture
async def client(session_factory, auth_redis):
    # Приложение поднято in-process, lifespan через ASGITransport не выполняется,
    # поэтому клиент Redis подставляется вручную. Без него проверка денилиста при
    # on_error='deny' считала бы отозванным ЛЮБОЙ токен.
    redis_module.auth_redis = auth_redis

    async def override_get_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    try:
        # X-Request-Id обязателен из-за RequestIdMiddleware (в проде его ставит Nginx).
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
            headers={'X-Request-Id': 'test-request-id'},
        ) as ac:
            yield ac
    finally:
        app.dependency_overrides.clear()
        redis_module.auth_redis = None
