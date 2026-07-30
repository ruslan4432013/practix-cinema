"""Точка входа сервиса пользовательского контента.

ЧЕМ ЭТОТ СЕРВИС ОТЛИЧАЕТСЯ ОТ КОЛЛЕКТОРА, И ПОЧЕМУ ОН ОТДЕЛЬНЫЙ.
``analytics-collector`` построен на инвариантe «ingest никогда не отдаёт 5xx»:
Kafka недоступна — событие уходит в Redis-буфер, Redis тоже — событие
отбрасывается с метрикой. Это допустимо ровно потому, что аналитическое событие
можно потерять. Оценку потерять нельзя: ответ «принято» обязан означать
«сохранено». Семантика прямо противоположная, поэтому эти ручки не могли
поселиться в коллекторе (разбор — в research/ugc-storage/README.md).

Ограничение частоты запросов здесь НЕ реализовано в приложении, и это решение,
а не пропуск. В репозитории уже два разошедшихся лимитера — фиксированное окно в
Auth и скользящее в коллекторе; третья копия ничего бы не добавила, кроме
дублирования. UGC-маршруты ограничиваются зоной ``ugc_write`` в Nginx, через
который они и приходят.
"""

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from practix_core.jwt import (
    install_config_loader,
    install_denylist_loader,
    install_exception_handler,
    make_jwt_settings,
)
from practix_core.openapi import install_bearer_security
from practix_core.sentry import init_sentry
from practix_ugc_api.api.v1 import bookmarks, health, likes, ratings, reviews
from practix_ugc_api.core.config import settings
from practix_ugc_api.core.logger import LOGGING, setup_logging
from practix_ugc_api.core.request_id import RequestIdMiddleware
from practix_ugc_api.core.tracing import init_tracer_provider, instrument_app
from practix_ugc_api.db import redis as redis_db

setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Трассировка инициализируется в каждом воркере отдельно (свой процесс).
    init_tracer_provider()
    # Сбор ошибок — там же, по одному фоновому потоку отправки на воркер.
    init_sentry(
        enabled=settings.SENTRY_ENABLED,
        dsn=settings.SENTRY_DSN,
        service_name=settings.OTEL_SERVICE_NAME,
        environment=settings.SENTRY_ENVIRONMENT,
        release=settings.SENTRY_RELEASE,
        sample_rate=settings.SENTRY_SAMPLE_RATE,
        send_default_pii=settings.SENTRY_SEND_DEFAULT_PII,
    )

    for problem in settings.insecure_defaults:
        logger.warning('INSECURE DEFAULT: %s', problem)

    redis_db.auth_redis = redis_db.create_auth_redis()
    yield
    if redis_db.auth_redis is not None:
        await redis_db.auth_redis.aclose()
        redis_db.auth_redis = None


app = FastAPI(
    title='UGC API — оценки, закладки и рецензии',
    description=(
        'Пользовательский контент онлайн-кинотеатра: оценки фильмов (целое 0..10, где лайк и '
        'дизлайк — частные случаи 10 и 0), закладки «посмотреть позже» и рецензии с голосованием '
        'за их полезность.\n\n'
        '**Запись требует авторизации.** `user_id` берётся исключительно из подписи токена и не '
        'может быть передан в теле запроса. Подпись проверяется локально, по общему с Auth '
        'секрету, — недоступность Auth не мешает ставить оценки.\n\n'
        '**Ответ означает сохранение.** В отличие от сервиса сбора аналитики, здесь запись не '
        'подтверждается до фактического коммита в PostgreSQL.\n\n'
        'Выбор хранилища и замеры — `research/ugc-storage/README.md`.'
    ),
    version='1.0.0',
    # `/api/openapi` за Nginx занят Movies API, `/api/analytics/openapi` — коллектором.
    docs_url='/api/ugc/openapi',
    openapi_url='/api/ugc/openapi.json',
    lifespan=lifespan,
)

app.add_middleware(RequestIdMiddleware)
instrument_app(app)

# Обвязка JWT — из practix_core.jwt. Секрет общий с Auth: токены здесь только
# проверяются, но никогда не выпускаются, поэтому сетевых обращений к Auth на
# горячем пути нет.
#
# on_error='deny', а НЕ 'allow' как у коллектора. Там недоступность Redis
# трактуется как «токен не отозван», потому что потерять событие аналитики хуже,
# чем принять его по отозванному токену. Здесь размен обратный: принять запись от
# имени пользователя, который вышел из системы, хуже, чем отказать. 'raise' тоже
# не подходит — он превратил бы недоступность Redis в 500 вместо осмысленного 401.
install_config_loader(
    lambda: make_jwt_settings(
        authjwt_secret_key=settings.AUTHJWT_SECRET_KEY,
        authjwt_denylist_enabled=settings.AUTHJWT_DENYLIST_ENABLED,
        authjwt_denylist_token_checks=settings.AUTHJWT_DENYLIST_TOKEN_CHECKS,
    )
)
install_denylist_loader(redis_db.get_auth_redis, on_error='deny')
install_exception_handler(app)

app.include_router(likes.router, prefix='/api/v1/likes', tags=['Оценки'])
app.include_router(ratings.router, prefix='/api/v1/ratings', tags=['Рейтинг фильма'])
app.include_router(bookmarks.router, prefix='/api/v1/bookmarks', tags=['Закладки'])
app.include_router(reviews.router, prefix='/api/v1/reviews', tags=['Рецензии'])
app.include_router(health.router, prefix='/health', tags=['Служебные'])

# Схема Bearer в документе OpenAPI — иначе из Swagger UI не послать токен.
install_bearer_security(app)


def run() -> None:
    """Точка входа для локальной отладки."""
    uvicorn.run('practix_ugc_api.main:app', host='0.0.0.0', port=8000, log_config=LOGGING)


if __name__ == '__main__':
    run()
