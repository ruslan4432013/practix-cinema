"""Асинхронный движок и сессии SQLAlchemy.

``expire_on_commit=False`` — чтобы объекты оставались пригодными к сериализации
после коммита. Пул ограничен явно: у сервиса несколько воркеров uvicorn, и пул
по умолчанию (5+10 на процесс) в сумме упирается в ``max_connections``.
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
