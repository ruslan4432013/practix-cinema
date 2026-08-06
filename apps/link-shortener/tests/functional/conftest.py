"""Фикстуры функционального набора.

Схема разворачивается **миграцией**, а не ``Base.metadata.create_all``: она
описана ручным DDL (CHECK-ограничения, частичные индексы), и ``create_all``
собрал бы схему из моделей — то есть НЕ ту, на которой сервис работает.
Заодно миграция оказывается под тестом на каждом прогоне.

Auth подменяется заглушкой через ``dependency_overrides``: набор проверяет
поведение шортенера, а не Auth, и поднимать его ради этого не нужно. Ровно тем
же способом набор ugc-api подменяет сессию.
"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from practix_link_shortener.api.v1.dependencies import get_auth_client, get_session_factory
from practix_link_shortener.core.config import settings
from practix_link_shortener.db.postgres import get_session
from practix_link_shortener.main import app

ALEMBIC_INI = Path(__file__).resolve().parents[2] / 'alembic.ini'


class FakeAuth:
    """Заглушка Auth: считает вызовы и умеет падать нужной ошибкой."""

    def __init__(self):
        self.calls: list[str] = []
        self.raises: Exception | None = None
        self.status = 'confirmed'

    async def confirm_email(self, user_id):
        self.calls.append(str(user_id))
        if self.raises is not None:
            raise self.raises
        return self.status


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
        await conn.execute(text('TRUNCATE short_link'))
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
def fake_auth() -> FakeAuth:
    return FakeAuth()


@pytest.fixture
async def client(session_factory, fake_auth):
    async def override_get_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_auth_client] = lambda: fake_auth
    # Фабрика для фонового учёта визитов. Без подмены он взял бы модульный
    # движок, чей пул соединений привязан к тому event loop, в котором
    # создавался, — а у набора цикл на каждый тест свой.
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    try:
        # follow_redirects=False обязателен: набор проверяет САМ редирект —
        # его код, Location и заголовки кэширования.
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://localhost',
            follow_redirects=False,
        ) as ac:
            yield ac
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def internal_headers() -> dict[str, str]:
    return {'Authorization': f'Bearer {settings.SHORTENER_INTERNAL_TOKEN}'}
