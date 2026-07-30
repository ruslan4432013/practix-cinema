import os
import sys

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Add auth/src to sys.path so we can import the application as `main`, `db.*`, etc.
_AUTH_SRC = os.environ.get('AUTH_SRC_PATH', '/opt/app/auth/src')
if _AUTH_SRC not in sys.path:
    sys.path.insert(0, _AUTH_SRC)

from main import app

from core.config import settings
from db.postgres import get_session
from db.redis import get_redis
from models.base import Base

DATABASE_URL = f'postgresql+asyncpg://{settings.AUTH_POSTGRES_USER}:{settings.AUTH_POSTGRES_PASSWORD}@{settings.AUTH_POSTGRES_HOST}:{settings.AUTH_POSTGRES_PORT}/{settings.AUTH_POSTGRES_DB}'

# Per-test engine/sessionmaker — re-created for each test to avoid
# "Event loop is closed" issues with pytest-asyncio (each test runs in its own loop).
engine = None
TestingSessionLocal = None


@pytest.fixture(scope='function', autouse=True)
async def setup_db():
    global engine, TestingSessionLocal
    engine = create_async_engine(DATABASE_URL, poolclass=pool.NullPool)
    TestingSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        # create_all триггерит after_create-листенер login_history, который
        # создаёт двухуровневые секции (год -> устройство), поэтому вручную
        # секции заводить не нужно.
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def client():
    from redis.asyncio import Redis

    import db.redis as redis_module

    # Reset the module-level redis singleton so it's bound to the current test's event loop.
    redis_module.redis_client = None
    test_redis = Redis(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        decode_responses=True,
    )

    async def override_get_session():
        async with TestingSessionLocal() as session:
            yield session

    async def override_get_redis():
        return test_redis

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_redis] = override_get_redis

    # Сбрасываем счётчики rate limit, чтобы общий (на IP) счётчик не протекал
    # между тестами — все тесты ходят в один Redis с одного тестового IP.
    ratelimit_keys = await test_redis.keys('ratelimit:*')
    if ratelimit_keys:
        await test_redis.delete(*ratelimit_keys)

    transport = ASGITransport(app=app)
    try:
        # X-Request-Id обязателен из-за RequestIdMiddleware (его в проде ставит Nginx).
        async with AsyncClient(
            transport=transport,
            base_url='http://test',
            headers={'X-Request-Id': 'test-request-id'},
        ) as ac:
            yield ac
    finally:
        app.dependency_overrides.clear()
        await test_redis.aclose()
        redis_module.redis_client = None
