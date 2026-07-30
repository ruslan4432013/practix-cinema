"""Точка входа сервиса сбора пользовательских действий.

Порядок middleware имеет значение. Starlette выполняет их в порядке,
обратном добавлению, поэтому запрос проходит их так:

    OTel → BodySizeLimit → RateLimit → RequestId → CORS → маршрут

* ``BodySizeLimit`` стоит первым из наших: отсечь гигантское тело нужно раньше
  всего остального, пока оно не занято память и не потрачено время.
* ``RateLimit`` — до формирования идентификатора запроса: отказ по лимиту не
  должен стоить дороже, чем одна операция в Redis.
* ``RequestId`` — последним, чтобы идентификатор был доступен и логам, и
  обработчику, и продюсеру Kafka.
* Инструментация OTel добавляется после всех, то есть оказывается самой
  внешней: серверный span должен покрывать в том числе отказы по лимиту.
"""

import logging
from contextlib import asynccontextmanager

import uvicorn
from async_fastapi_jwt_auth import AuthJWT
from async_fastapi_jwt_auth.exceptions import AuthJWTException
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from api.v1 import events, health
from brokers.kafka import KafkaEventBroker
from core.body_limit import BodySizeLimitMiddleware
from core.config import settings
from core.logger import LOGGING, setup_logging
from core.rate_limit import RateLimitMiddleware
from core.request_id import RequestIdMiddleware
from core.tracing import init_tracer_provider, instrument_app
from db import redis as redis_db
from services import providers
from services.event_service import EventService
from services.fallback_buffer import FallbackBuffer

setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Трассировка инициализируется в каждом воркере отдельно (свой процесс).
    init_tracer_provider()

    # Значения по умолчанию у секретов опасны тем, что работают: сервис
    # поднимается, ничего не ломается, и подмену забывают. В продакшене такой
    # старт запрещён (core/config.py), вне продакшена — обязан быть заметным.
    for problem in settings.insecure_defaults:
        logger.warning('INSECURE DEFAULT: %s', problem)

    redis_db.redis = redis_db.create_redis()
    redis_db.auth_redis = redis_db.create_auth_redis()

    # Буфер и брокер ссылаются друг на друга: брокер отдаёт в буфер записи,
    # доставка которых провалилась, а буфер публикует их обратно при дренаже.
    # Цикл разрывается поздним связыванием колбэка.
    broker = KafkaEventBroker()
    buffer = FallbackBuffer(redis_db.get_redis, broker)
    broker.set_delivery_failure_handler(buffer.push)

    providers.broker = broker
    providers.fallback_buffer = buffer
    providers.event_service = EventService(broker, buffer, redis_db.get_redis)

    # Недоступность Kafka на старте не мешает подъёму: сервис работает в
    # degraded-режиме и складывает события в буфер (см. brokers/kafka.py).
    await broker.start()
    buffer.start()

    yield

    await buffer.stop()
    await broker.stop()
    if redis_db.redis is not None:
        await redis_db.redis.aclose()
        redis_db.redis = None
    if redis_db.auth_redis is not None:
        await redis_db.auth_redis.aclose()
        redis_db.auth_redis = None
    providers.broker = None
    providers.fallback_buffer = None
    providers.event_service = None


app = FastAPI(
    title='Analytics Collector — сбор пользовательских действий',
    description=(
        'Сервис приёма пользовательских событий онлайн-кинотеатра: клики, просмотры страниц '
        'и кастомные события плеера и поиска. События обогащаются на сервере и публикуются '
        'в Kafka.\n\n'
        '**Аутентификация опциональна.** При наличии валидного Bearer-токена событие '
        'атрибутируется пользователю, иначе считается анонимным. Поле `user_id` берётся '
        'только из токена и не может быть передано в теле запроса.\n\n'
        '**Отказоустойчивость.** Недоступность Kafka не приводит к ошибке: события '
        'складываются в буфер и доставляются фоновым дренажом после восстановления.'
    ),
    version='1.0.0',
    # Путь документации отличается от `/api/openapi`: тот за Nginx уже занят
    # Movies API (см. nginx/configs/site.conf).
    docs_url='/api/analytics/openapi',
    openapi_url='/api/analytics/openapi.json',
    lifespan=lifespan,
)

# Порядок добавления обратен порядку выполнения — см. docstring модуля.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    # Токен передаётся заголовком Authorization, cookie не используются,
    # поэтому разрешать передачу учётных данных не нужно.
    allow_credentials=False,
    allow_methods=['POST', 'GET', 'OPTIONS'],
    allow_headers=['Authorization', 'Content-Type', 'X-Request-Id'],
    max_age=600,
)
app.add_middleware(RequestIdMiddleware)
app.add_middleware(RateLimitMiddleware, redis_provider=redis_db.get_redis)
app.add_middleware(BodySizeLimitMiddleware, max_body_bytes=settings.UGC_MAX_BODY_BYTES)
instrument_app(app)


class JWTSettings(BaseModel):
    """Конфигурация проверки JWT.

    Секрет общий с Auth-сервисом: токены только проверяются здесь, но никогда
    не выпускаются, поэтому сетевые обращения к Auth на горячем пути не нужны.
    """

    authjwt_secret_key: str = settings.AUTHJWT_SECRET_KEY
    authjwt_denylist_enabled: bool = settings.AUTHJWT_DENYLIST_ENABLED
    authjwt_denylist_token_checks: set = settings.AUTHJWT_DENYLIST_TOKEN_CHECKS


@AuthJWT.load_config
def get_jwt_config():
    return JWTSettings()


@AuthJWT.token_in_denylist_loader
async def check_if_token_in_denylist(decrypted_token) -> bool:
    """Проверяет, отозван ли токен.

    Читаем ИМЕННО из Redis Auth-сервиса (``get_auth_redis``), а не из своего:
    денилист ведёт Auth, и в базе коллектора этих ключей нет.

    Недоступность Redis трактуется как «токен не отозван»: событие аналитики не
    стоит того, чтобы терять его из-за проблем с кэшем. Риск ограничен — по
    отозванному токену можно лишь отправить событие от своего же имени.
    """
    redis = redis_db.get_auth_redis()
    if redis is None:
        return False
    try:
        return await redis.get(decrypted_token['jti']) is not None
    except Exception as exc:  # noqa: BLE001
        logger.warning('Denylist check skipped, Redis unavailable: %s', exc)
        return False


@app.exception_handler(AuthJWTException)
def authjwt_exception_handler(_request: Request, exc: AuthJWTException):
    """Обработчик ошибок JWT.

    Срабатывает редко: ingest-ручки используют ``jwt_optional`` и сами
    подавляют проблемы с токеном, трактуя событие как анонимное.
    """
    return JSONResponse(status_code=exc.status_code, content={'detail': exc.message})


app.include_router(events.router, prefix='/api/v1/events', tags=['События'])
app.include_router(health.router, prefix='/health', tags=['Служебные'])
app.include_router(health.metrics_router, tags=['Служебные'])


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    openapi_schema.setdefault('components', {})['securitySchemes'] = {
        'Bearer': {'type': 'http', 'scheme': 'bearer', 'bearerFormat': 'JWT'},
    }
    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi


if __name__ == '__main__':
    uvicorn.run('main:app', host='0.0.0.0', port=8000, log_config=LOGGING)
