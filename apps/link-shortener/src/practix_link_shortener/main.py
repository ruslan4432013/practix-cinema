"""Точка входа сервиса сокращения ссылок.

ЗАЧЕМ ОТДЕЛЬНЫЙ СЕРВИС. Чек-лист спринта называет сокращение ссылок отдельным
компонентом, и это не формальность: у него своя таблица, свой срок жизни данных,
свой профиль нагрузки (один поиск по первичному ключу на переход) и своя
публичная поверхность — единственный маршрут, который открывает браузер по
ссылке из письма. Внутри нотификаций он оказался бы шестым Django-приложением в
и без того самом крупном сервисе репозитория, и им не смог бы пользоваться
никто, кроме рассылки.

ОБВЯЗКА ЛОГОВ И ТРАССИРОВКИ СОБРАНА ЗДЕСЬ, А НЕ В core/logger.py И
core/tracing.py. В UGC это два отдельных модуля, и они почти дословно повторяют
такие же в других сервисах — третья копия упёрлась бы в жёсткий порог jscpd
(0.8%). Вызовы ``practix_core`` короткие, прятать их за модулем-обёрткой нечего.
"""

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from practix_core.logging import build_logging_config, setup_logging
from practix_core.request_id import RequestIdMiddleware
from practix_core.sentry import init_sentry_from_env
from practix_core.tracing import init_tracer_provider, instrument_fastapi
from practix_link_shortener.api.v1 import dependencies, health, links, redirect
from practix_link_shortener.core.config import settings
from practix_link_shortener.services.auth_client import AuthClient

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
    # Auth, а не с ETL, и полная трассировка дешевле потери контекста инцидента.
    init_tracer_provider(
        enabled=settings.OTEL_ENABLED,
        service_name=settings.OTEL_SERVICE_NAME,
        endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT,
    )
    # Из окружения, а не семью аргументами из настроек: ключи SENTRY_* идут без
    # префикса сервиса (приёмник один на стенд), и блок был бы седьмой копией
    # одного вызова. Настройки те же самые — pydantic читает то же окружение.
    init_sentry_from_env(service_name=settings.OTEL_SERVICE_NAME)

    for problem in settings.insecure_defaults:
        logger.warning('INSECURE DEFAULT: %s', problem)

    dependencies.auth_client = AuthClient()
    yield
    if dependencies.auth_client is not None:
        await dependencies.auth_client.aclose()
        dependencies.auth_client = None


app = FastAPI(
    title='Link Shortener — короткие ссылки и подтверждение email',
    description=(
        'Сокращает ссылки для писем и подтверждает по ним адрес электронной почты.\n\n'
        '**Короткий код — непрозрачный ключ.** За ним стоят идентификатор пользователя (чтобы '
        'считать визиты), срок действия и целевой адрес `redirectUrl`. Класть их в саму ссылку '
        'параметрами значило бы получить строку в ~200 символов — то есть отменить сокращение, '
        'ради которого всё и затевалось.\n\n'
        '**Просроченная, отозванная и несуществующая ссылка отвечают одинаково** — страницей 404. '
        'Различать их снаружи — значит подсказывать перебирающему, какие коды существуют.\n\n'
        '`POST /api/v1/links` маршрута в nginx не имеет: ручка межсервисная и снаружи сети '
        'недостижима по построению.'
    ),
    version='1.0.0',
    # `/api/openapi` за nginx занят Movies API, `/api/analytics/openapi` — коллектором,
    # `/api/ugc/openapi` — сервисом UGC.
    docs_url='/api/shortener/openapi',
    openapi_url='/api/shortener/openapi.json',
    lifespan=lifespan,
)

# on_missing='generate', а НЕ умолчание reject_400 (см. core/request_id.py):
# `/s/{code}` открывает браузер, и заголовка там нет.
app.add_middleware(RequestIdMiddleware, on_missing='generate')
instrument_fastapi(app, enabled=settings.OTEL_ENABLED, excluded_urls='health.*')

# Публичный маршрут висит на том же префиксе, что и `location ^~ /s/` в nginx:
# короткая ссылка выглядит как http://localhost/s/AbC1234.
app.include_router(redirect.router, prefix=f'/{settings.SHORTENER_REDIRECT_PATH.strip("/")}')
app.include_router(links.router, prefix='/api/v1/links', tags=['Ссылки'])
app.include_router(health.router, prefix='/health', tags=['Служебные'])


def run() -> None:
    """Точка входа для локальной отладки."""
    uvicorn.run('practix_link_shortener.main:app', host='0.0.0.0', port=8000, log_config=LOGGING)


if __name__ == '__main__':
    run()
