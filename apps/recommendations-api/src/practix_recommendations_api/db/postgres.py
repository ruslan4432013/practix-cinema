"""Асинхронный движок и сессии SQLAlchemy для витрины.

Пул ограничен явно: сервис держит четыре воркера uvicorn, и пул по умолчанию
(5+10 на процесс) в сумме упирается в ``max_connections`` PostgreSQL.

``pool_pre_ping`` обязателен: витрина — не горячий путь всех запросов (первым
идёт Redis), поэтому соединение может простоять достаточно, чтобы его закрыл
сервер, и промах кэша упёрся бы в мёртвый коннект ровно тогда, когда он нужен.

ТАЙМАУТЫ ЗДЕСЬ — ЧАСТЬ ЛЕСТНИЦЫ ДЕГРАДАЦИИ, А НЕ ТЮНИНГ. Отказ базы обязан
обнаруживаться быстрее, чем nginx обрывает запрос: иначе `except` в
``services/degradation.py`` не выполнится вовсе, потому что исключения не
будет — будет ожидание. Проверено на стенде: без них остановленный контейнер
recs-db давал 504 вместо 200 с популярным.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from practix_recommendations_api.core.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=settings.RECS_DB_ECHO,
    future=True,
    pool_size=settings.RECS_DB_POOL_SIZE,
    max_overflow=settings.RECS_DB_MAX_OVERFLOW,
    pool_pre_ping=True,
    # Ожидание свободного соединения в пуле — тоже ожидание: под нагрузкой на
    # мёртвой базе все слоты заняты висящими запросами.
    pool_timeout=settings.RECS_DB_TIMEOUT,
    connect_args={
        # timeout ограничивает УСТАНОВКУ соединения (мёртвый или недоступный
        # хост), command_timeout — выполнение запроса (живой сервер, который
        # перестал отвечать). Это разные отказы, и одного параметра мало.
        'timeout': settings.RECS_DB_TIMEOUT,
        'command_timeout': settings.RECS_DB_TIMEOUT,
    },
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession]:
    async with async_session() as session:
        yield session
