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
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from practix_analytics_collector.api.v1 import events, health
from practix_analytics_collector.brokers.kafka import KafkaEventBroker
from practix_analytics_collector.core.body_limit import BodySizeLimitMiddleware
from practix_analytics_collector.core.config import settings
from practix_analytics_collector.core.logger import LOGGING, setup_logging
from practix_analytics_collector.core.rate_limit import RateLimitMiddleware
from practix_analytics_collector.core.request_id import RequestIdMiddleware
from practix_analytics_collector.core.tracing import init_tracer_provider, instrument_app
from practix_analytics_collector.db import redis as redis_db
from practix_analytics_collector.services import providers
from practix_analytics_collector.services.event_service import EventService
from practix_analytics_collector.services.fallback_buffer import FallbackBuffer
from practix_core.jwt import (
    install_config_loader,
    install_denylist_loader,
    install_exception_handler,
    make_jwt_settings,
)
from practix_core.openapi import install_bearer_security
from practix_core.sentry import init_sentry

setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Трассировка инициализируется в каждом воркере отдельно (свой процесс).
    init_tracer_provider()
    # Сбор ошибок — там же, по одному фоновому потоку отправки на воркер.
    # Инвариант «ingest никогда не отдаёт 5xx» не задет: SDK ставит событие в
    # очередь и при недоступности приёмника молча его отбрасывает.
    init_sentry(
        enabled=settings.SENTRY_ENABLED,
        dsn=settings.SENTRY_DSN,
        service_name=settings.OTEL_SERVICE_NAME,
        environment=settings.SENTRY_ENVIRONMENT,
        release=settings.SENTRY_RELEASE,
        sample_rate=settings.SENTRY_SAMPLE_RATE,
        send_default_pii=settings.SENTRY_SEND_DEFAULT_PII,
    )

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
# on_missing='generate', а не 400 как у rest/auth: ручка публичная и вызывается
# из браузера через navigator.sendBeacon, который не умеет ставить произвольные
# заголовки — требование X-Request-Id отсекло бы beacon-события целиком.
# sanitize=True: значение может прийти напрямую от клиента и уезжает в лог и в
# заголовок Kafka, поэтому усекается и очищается от управляющих символов.
app.add_middleware(RequestIdMiddleware, on_missing='generate', sanitize=True)
app.add_middleware(RateLimitMiddleware, redis_provider=redis_db.get_redis)
app.add_middleware(BodySizeLimitMiddleware, max_body_bytes=settings.UGC_MAX_BODY_BYTES)
instrument_app(app)


# Обвязка JWT переехала в practix_core.jwt.
#
# ДВА ОТЛИЧИЯ ОТ rest/auth, оба сохранены и оба выражены аргументами:
#
# 1. Денилист читается из Redis АУТ-СЕРВИСА (get_auth_redis), а не из своего:
#    денилист ведёт Auth, и в базе коллектора этих ключей просто нет. Один общий
#    клиент искал бы их не там — ровно та ошибка, ради которой клиенты разделены.
# 2. on_error='allow' — недоступность Redis трактуется как «токен не отозван».
#    Событие аналитики не стоит того, чтобы терять его из-за проблем с кэшем, а
#    риск ограничен: по отозванному токену можно лишь отправить событие от своего
#    же имени. У Auth такой выбор был бы регрессией безопасности, поэтому в
#    библиотеке по умолчанию стоит 'raise', а не это значение.
#
# Секрет общий с Auth: токены здесь только проверяются, но никогда не
# выпускаются, поэтому сетевые обращения к Auth на горячем пути не нужны.
install_config_loader(
    lambda: make_jwt_settings(
        authjwt_secret_key=settings.AUTHJWT_SECRET_KEY,
        authjwt_denylist_enabled=settings.AUTHJWT_DENYLIST_ENABLED,
        authjwt_denylist_token_checks=settings.AUTHJWT_DENYLIST_TOKEN_CHECKS,
    )
)
install_denylist_loader(redis_db.get_auth_redis, on_error='allow')
install_exception_handler(app)


app.include_router(events.router, prefix='/api/v1/events', tags=['События'])
app.include_router(health.router, prefix='/health', tags=['Служебные'])
app.include_router(health.metrics_router, tags=['Служебные'])

# Схема Bearer в документе OpenAPI: без неё Swagger UI не даёт послать токен.
# Тело переехало в practix_core.openapi — оно было побайтово одинаковым здесь и
# в ugc-api, и это обязательный бойлерплейт, а не решение этого сервиса.
install_bearer_security(app)


def run() -> None:
    """Точка входа консольной команды ``analytics-collector`` (локальная отладка)."""
    uvicorn.run('practix_analytics_collector.main:app', host='0.0.0.0', port=8000, log_config=LOGGING)


if __name__ == '__main__':
    run()
