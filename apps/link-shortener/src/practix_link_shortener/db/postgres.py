"""Асинхронный движок и сессии SQLAlchemy.

``expire_on_commit=False`` — чтобы объекты оставались пригодными к сериализации
после коммита.

ПУЛ ЗДЕСЬ — НА ПРОЦЕСС, А ЛИМИТ БАЗЫ — НА ВЕСЬ СЕРВИС, и вся ловушка в этом
расхождении. Сервис держит четыре воркера uvicorn, у каждого свой пул, поэтому
shortener-db видит ``4 × (pool_size + max_overflow)``. Ограничить пул «явно»
мало — его надо ограничить ЧИСЛОМ, поделённым на воркеров: прежний докстринг
обещал ровно это, а стоявшие рядом 10 + 20 давали 120 соединений против
умолчания postgres в 100. Здесь это дороже, чем в соседних сервисах:
``/s/{code}`` — маршрут из письма, и `too many connections` человек читает как
«ссылка не работает».

Арифметика бюджета shortener-db записана у самой базы
(infra/compose/docker-compose.yml) и закреплена тестом
apps/link-shortener/tests/unit/test_connection_budget.py: он читает
``--workers`` из Dockerfile и ``max_connections`` из compose, так что изменить
одно и забыть другое молча не выйдет.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from practix_link_shortener.core.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=settings.SHORTENER_DB_ECHO,
    future=True,
    pool_size=settings.SHORTENER_DB_POOL_SIZE,
    max_overflow=settings.SHORTENER_DB_MAX_OVERFLOW,
    pool_pre_ping=True,
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession]:
    async with async_session() as session:
        yield session
