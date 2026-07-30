import logging
from contextlib import asynccontextmanager

import httpx
import uvicorn
from async_fastapi_jwt_auth import AuthJWT
from async_fastapi_jwt_auth.exceptions import AuthJWTException
from elasticsearch import AsyncElasticsearch
from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from redis.asyncio import ConnectionPool, Redis

from api.v1 import films, genres, persons
from core import config
from core.logger import LOGGING
from core.request_id import RequestIdMiddleware
from core.tracing import init_tracer_provider, instrument_app
from db import elastic_db, redis_db
from services import auth_client


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


class JWTSettings(BaseModel):
    authjwt_secret_key: str = config.settings.authjwt_secret_key
    authjwt_denylist_enabled: bool = config.settings.authjwt_denylist_enabled
    authjwt_denylist_token_checks: set = config.settings.authjwt_denylist_token_checks


@AuthJWT.load_config
def get_config():
    """Загрузка конфигурации JWT."""
    return JWTSettings()


@AuthJWT.token_in_denylist_loader
async def check_if_token_in_denylist(decrypted_token):
    """Проверка наличия токена в списке отозванных."""
    jti = decrypted_token['jti']
    return await redis_db.redis.get(jti) is not None


@app.exception_handler(AuthJWTException)
def authjwt_exception_handler(request: Request, exc: AuthJWTException):
    """Обработчик исключений JWT."""
    return JSONResponse(status_code=exc.status_code, content={'detail': exc.message})


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
    openapi_schema['components']['securitySchemes'] = {
        'Bearer': {
            'type': 'http',
            'scheme': 'bearer',
            'bearerFormat': 'JWT',
        }
    }
    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi

if __name__ == '__main__':
    # Приложение может запускаться командой
    # `uvicorn main:app --host 0.0.0.0 --port 8000`
    # но чтобы не терять возможность использовать дебагер,
    # запустим uvicorn-сервер через python
    uvicorn.run(
        'main:app',
        host='0.0.0.0',
        port=8000,
        log_config=LOGGING,
        log_level=logging.DEBUG,
    )
