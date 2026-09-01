"""Точка входа сервиса выдачи рекомендаций.

ЗАЧЕМ ОТДЕЛЬНЫЙ СЕРВИС, А НЕ РУЧКА В MOVIES API (ADR-006). У рекомендаций
другой профиль отказа: им МОЖНО деградировать до популярного, каталогу —
нельзя. Другое хранилище: витрина перезаписывается батчем целиком, и такие
записи не должны стоять рядом с горячим чтением карточек. Другой темп релизов:
модель меняется чаще, чем каталог. И главное — ручка внутри Movies API сделала
бы каталог зависимым от готовности модели, то есть страница фильма падала бы
из-за блока рекомендаций. Ровно тот же аргумент, по которому websocket-шлюз в
этом репозитории живёт отдельно от панели нотификаций.

ОБВЯЗКА ЛОГОВ И ТРАССИРОВКИ СОБРАНА ЗДЕСЬ, а не в core/logger.py и
core/tracing.py, как в UGC. Те два модуля почти дословно повторяют такие же в
соседних сервисах, и очередная копия упёрлась бы в жёсткий порог jscpd (0.8%).
Вызовы ``practix_core`` короткие, прятать их за модулем-обёрткой нечего — тот
же выбор сделан в link-shortener.
"""

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from practix_core.health import reachable
from practix_core.jwt import (
    install_config_loader,
    install_denylist_loader,
    install_exception_handler,
    make_jwt_settings,
)
from practix_core.logging import build_logging_config, setup_logging
from practix_core.openapi import install_bearer_security
from practix_core.request_id import RequestIdMiddleware
from practix_core.sentry import init_sentry_from_env
from practix_core.tracing import init_tracer_provider, instrument_fastapi, instrument_httpx
from practix_recommendations_api.api.v1 import dependencies, health, recommendations
from practix_recommendations_api.core.config import settings
from practix_recommendations_api.db import redis as redis_db
from practix_recommendations_api.services.cache import ShelfCache
from practix_recommendations_api.services.catalog_client import CatalogClient

LOGGING = build_logging_config(
    level=settings.LOG_LEVEL,
    json_output=settings.LOG_JSON,
    # Имя сервиса в каждой записи — то же OTEL_SERVICE_NAME, что у трассировки:
    # логи, трейсы и ошибки должны называть сервис одинаково.
    static_fields={'service': settings.OTEL_SERVICE_NAME},
    logger_levels={
        'uvicorn': settings.LOG_LEVEL,
        'uvicorn.error': settings.LOG_LEVEL,
        'uvicorn.access': settings.LOG_LEVEL,
        # SQLAlchemy на INFO печатает каждый запрос и его параметры.
        'sqlalchemy.engine': 'WARNING',
    },
)
setup_logging(LOGGING)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Трассировка и сбор ошибок инициализируются в каждом воркере отдельно
    # (свой процесс). Сэмплирование не включается: поток запросов сопоставим с
    # Movies API, а не с ETL, и полная трассировка дешевле потери контекста.
    init_tracer_provider(
        enabled=settings.OTEL_ENABLED,
        service_name=settings.OTEL_SERVICE_NAME,
        endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT,
    )
    instrument_httpx()
    init_sentry_from_env(service_name=settings.OTEL_SERVICE_NAME)

    for problem in settings.insecure_defaults:
        logger.warning('INSECURE DEFAULT: %s', problem)

    redis_db.recs_redis = redis_db.create_recs_redis()
    redis_db.auth_redis = redis_db.create_auth_redis()
    dependencies.shelf_cache = ShelfCache(redis_db.recs_redis)
    dependencies.catalog_client = CatalogClient()

    # Недоступность любого Redis на старте — предупреждение, а не отказ
    # подняться: горячий слой заменяется базой, а денилист при политике `deny`
    # просто отвергнет персональные запросы. Анонимные ручки работают в обоих
    # случаях, и ронять из-за этого весь сервис было бы обменом наоборот.
    if not await reachable(redis_db.recs_redis, settings.RECS_REDIS_TIMEOUT):
        logger.warning('CACHE UNAVAILABLE: горячий слой недоступен, выдача пойдёт из PostgreSQL')
    if not await reachable(redis_db.auth_redis, settings.RECS_AUTH_REDIS_TIMEOUT):
        logger.warning('DENYLIST UNREACHABLE: персональная выдача будет отвергаться (политика deny)')

    yield

    if dependencies.catalog_client is not None:
        await dependencies.catalog_client.aclose()
        dependencies.catalog_client = None
    dependencies.shelf_cache = None
    for client in (redis_db.recs_redis, redis_db.auth_redis):
        if client is not None:
            await client.aclose()
    redis_db.recs_redis = None
    redis_db.auth_redis = None


app = FastAPI(
    title='Recommendations API — похожие фильмы, персональные подборки, популярное',
    description=(
        'Онлайн-выдача рекомендаций. **В запросе не считается ничего**: модель обучается ночным батчем '
        '`recsys-trainer`, а сервис только читает подготовленную витрину — иначе не уложиться в 300 мс.\n\n'
        '**Ни один отказ не превращается в 5xx.** Нет горячего слоя — читаем PostgreSQL; нет витрины — '
        'отдаём популярное; нет и его — 200 с пустым списком. Что именно отдано, всегда написано в поле '
        '`source` (`similar` / `personal` / `popular`): деградация, о которой не сказано, — это деградация, '
        'которую никто не заметит.\n\n'
        '**`user_id` берётся только из токена.** Параметра с чужим идентификатором нет и не будет: '
        'по персональной выдаче восстанавливается история просмотров.'
    ),
    version='1.0.0',
    # `/api/openapi` за nginx занят Movies API, `/api/analytics/openapi` — коллектором,
    # `/api/ugc/openapi` — сервисом UGC, `/api/shortener/openapi` — короткими ссылками.
    docs_url='/api/recommendations/openapi',
    openapi_url='/api/recommendations/openapi.json',
    lifespan=lifespan,
)

app.add_middleware(RequestIdMiddleware)
instrument_fastapi(app, enabled=settings.OTEL_ENABLED, excluded_urls='health.*,metrics')

install_config_loader(
    lambda: make_jwt_settings(
        authjwt_secret_key=settings.AUTHJWT_SECRET_KEY,
        authjwt_denylist_enabled=settings.AUTHJWT_DENYLIST_ENABLED,
        authjwt_denylist_token_checks=settings.AUTHJWT_DENYLIST_TOKEN_CHECKS,
    )
)
# `deny`, а не `allow` коллектора: раздел 5.5 ТЗ. Отозванный токен не должен
# продолжать читать личную выдачу, даже если денилист временно недоступен.
install_denylist_loader(redis_db.get_auth_redis, on_error='deny')
install_exception_handler(app)

app.include_router(recommendations.router, prefix='/api/v1/recommendations')
app.include_router(health.router, prefix='/health', tags=['Служебные'])
app.include_router(health.metrics_router, tags=['Служебные'])

install_bearer_security(app)


def run() -> None:
    """Точка входа для локальной отладки."""
    uvicorn.run('practix_recommendations_api.main:app', host='0.0.0.0', port=8000, log_config=LOGGING)


if __name__ == '__main__':
    run()
