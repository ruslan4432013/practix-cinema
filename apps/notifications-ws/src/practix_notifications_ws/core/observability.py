"""Логирование, трассировка и сбор ошибок в одном модуле.

У соседних FastAPI-сервисов это три файла (``core/logger.py``,
``core/tracing.py``, вызов ``init_sentry`` в ``main.py``). Здесь один, и причина
не в стиле: делить на три значило бы получить три файла по десять строк, каждый
из которых почти совпадает с одноимённым файлом UGC API, — а дублирование в
репозитории под жёстким гейтом. Всё, что различается у сервисов, — параметры,
и они собраны в одном месте.

``uvicorn.access`` НЕ приглушается: у шлюза запросов мало (handshake и
long-poll), и видеть, кто и когда подключался, полезно. А вот ``aio_pika`` и
``aiormq`` на INFO печатают каждое переподключение к брокеру построчно — им
поднят порог.
"""

import logging

from practix_core.logging import build_logging_config
from practix_core.logging import setup_logging as _setup_logging
from practix_core.sentry import init_sentry
from practix_core.tracing import init_tracer_provider, instrument_fastapi
from practix_notifications_ws.core.config import settings


def build_config() -> dict:
    return build_logging_config(
        level=settings.LOG_LEVEL,
        json_output=settings.LOG_JSON,
        static_fields={'service': settings.OTEL_SERVICE_NAME},
        logger_levels={
            'uvicorn': settings.LOG_LEVEL,
            'uvicorn.error': settings.LOG_LEVEL,
            'uvicorn.access': settings.LOG_LEVEL,
            'aio_pika': 'WARNING',
            'aiormq': 'WARNING',
        },
    )


LOGGING = build_config()


def setup_logging() -> None:
    _setup_logging(LOGGING)


def init_runtime_observability(app) -> None:
    """Трассировка и сбор ошибок. Зовётся из lifespan — один раз на процесс.

    Сэмплирование не включается: поток запросов у шлюза сопоставим с Auth, а не
    с ETL, и полный трейс дешевле потерянного контекста инцидента.

    ``excluded_urls`` снимает с трассировки пробы; websocket-маршрут инструментация
    FastAPI не трогает вовсе — спан на соединение, живущее часами, был бы
    бесполезен, а спан на кадр стоил бы дороже кадра.
    """
    init_tracer_provider(
        enabled=settings.OTEL_ENABLED,
        service_name=settings.OTEL_SERVICE_NAME,
        endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT,
    )
    instrument_fastapi(app, enabled=settings.OTEL_ENABLED, excluded_urls='health.*')
    init_sentry(
        enabled=settings.SENTRY_ENABLED,
        dsn=settings.SENTRY_DSN,
        service_name=settings.OTEL_SERVICE_NAME,
        environment=settings.SENTRY_ENVIRONMENT,
        release=settings.SENTRY_RELEASE or None,
        sample_rate=settings.SENTRY_SAMPLE_RATE,
        send_default_pii=settings.SENTRY_SEND_DEFAULT_PII,
    )

    logger = logging.getLogger('notifications_ws.observability')
    for problem in settings.insecure_defaults:
        logger.warning('Insecure default in use: %s', problem)
