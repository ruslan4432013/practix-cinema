import logging
from contextlib import asynccontextmanager

import httpx
import uvicorn
from elasticsearch import AsyncElasticsearch
from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from redis.asyncio import ConnectionPool, Redis

from practix_core.jwt import (
    install_config_loader,
    install_denylist_loader,
    install_exception_handler,
    make_jwt_settings,
)
from practix_movies_api.api.v1 import films, genres, persons
from practix_movies_api.core import config
from practix_movies_api.core.logger import LOGGING
from practix_movies_api.core.request_id import RequestIdMiddleware
from practix_movies_api.core.tracing import init_tracer_provider, instrument_app
from practix_movies_api.db import elastic_db, redis_db
from practix_movies_api.services import auth_client


@asynccontextmanager
async def lifespan(_app_instance: FastAPI):
    # Инициализируем трассировку на каждый воркер (в его собственном процессе).
    init_tracer_provider()
    # Подключаемся к базам при старте сервера
    # Подключиться можем при работающем event-loop
    # Поэтому логика подключения происходит в асинхронной функции
    redis_pool = ConnectionPool(
        host=config.settings.redis_host,
        port=config.settings.redis_port,
        max_connections=500,
    )
    redis_db.redis = Redis(connection_pool=redis_pool)
    elastic_db.es = AsyncElasticsearch(
        hosts=[f'{config.settings.elastic_schema}{config.settings.elastic_host}:{config.settings.elastic_port}'],
        max_retries=3,
        connections_per_node=32,
    )
    # Разделяемый HTTP-клиент для межсервисных запросов в Auth-сервис.
    auth_client.client = httpx.AsyncClient(timeout=config.settings.auth_request_timeout)
    yield
    # Отключаемся от баз при выключении сервера
    await redis_db.redis.close()
    await elastic_db.es.close()
    await auth_client.client.aclose()


app = FastAPI(
    title='Async API — Онлайн-кинотеатр',
    description=(
        'Read-only REST API для поиска фильмов, жанров и персон. '
        'Данные хранятся в Elasticsearch, ответы кэшируются в Redis. '
        'Источник данных — PostgreSQL, перенос осуществляется ETL-пайплайном.'
    ),
    version='1.0.0',
    # Адрес документации в красивом интерфейсе
    docs_url='/api/openapi',
    # Адрес документации в формате OpenAPI
    openapi_url='/api/openapi.json',
    lifespan=lifespan,
)

# Трассировка: сначала наш middleware (внутренний), затем инструментация OTel
# (внешняя) — чтобы серверный span уже существовал при простановке тега.
app.add_middleware(RequestIdMiddleware)
instrument_app(app)


# Обвязка JWT переехала в practix_core.jwt: JWTSettings, load_config,
# денилист и обработчик исключений существовали в трёх приложениях почти
# побайтово. Политика при отказе Redis сохранена прежняя ('raise'): менять её
# на fail-open — продуктовое решение, а не побочный эффект рефакторинга.
install_config_loader(
    lambda: make_jwt_settings(
        authjwt_secret_key=config.settings.authjwt_secret_key,
        authjwt_denylist_enabled=config.settings.authjwt_denylist_enabled,
        authjwt_denylist_token_checks=config.settings.authjwt_denylist_token_checks,
    )
)
install_denylist_loader(lambda: redis_db.redis)
install_exception_handler(app)


app.include_router(films.router, prefix='/api/v1/films', tags=['Фильмы'])
app.include_router(genres.router, prefix='/api/v1/genres', tags=['Жанры'])
app.include_router(persons.router, prefix='/api/v1/persons', tags=['Персоны'])


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    # setdefault, а не прямая индексация: ключ 'components' появляется в схеме
    # только если какой-нибудь роут объявил модель. Сейчас такие роуты есть, но
    # удаление последнего response_model превратило бы /api/openapi.json в 500 —
    # а его опрашивает healthcheck контейнера. Так же сделано в коллекторе.
    openapi_schema.setdefault('components', {})['securitySchemes'] = {
        'Bearer': {
            'type': 'http',
            'scheme': 'bearer',
            'bearerFormat': 'JWT',
        }
    }
    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi


def run() -> None:
    """Точка входа консольной команды ``movies-api``.

    В контейнере сервис поднимается через ``uvicorn`` с несколькими воркерами;
    эта функция нужна для локального запуска под отладчиком — импортная строка
    (а не объект ``app``) обязательна, иначе uvicorn не сможет перезагружать код.
    """
    uvicorn.run(
        'practix_movies_api.main:app',
        host='0.0.0.0',
        port=8000,
        log_config=LOGGING,
        log_level=logging.DEBUG,
    )


if __name__ == '__main__':
    run()
