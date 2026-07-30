from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from practix_auth.core.config import settings

DATABASE_URL = f'postgresql+asyncpg://{settings.AUTH_POSTGRES_USER}:{settings.AUTH_POSTGRES_PASSWORD}@{settings.AUTH_POSTGRES_HOST}:{settings.AUTH_POSTGRES_PORT}/{settings.AUTH_POSTGRES_DB}'

engine = create_async_engine(DATABASE_URL, echo=True, future=True)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session
