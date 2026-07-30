"""Настройка логирования сервиса UGC.

Реализация — в ``practix_core.logging``; здесь остаётся только то, что у
сервисов различается, и различается осознанно.

``uvicorn.access`` НЕ приглушается, в отличие от коллектора. Там ingest-ручку
дёргают на каждое действие пользователя, и построчный access-лог по объёму
сопоставим с самим потоком событий. Здесь запросов на порядки меньше, а
разбирать «кто и когда поставил оценку» по access-логу удобно.
"""

from practix_core.logging import build_logging_config
from practix_core.logging import setup_logging as _setup_logging
from practix_ugc_api.core.config import settings


def build_config() -> dict:
    return build_logging_config(
        level=settings.LOG_LEVEL,
        json_output=settings.LOG_JSON,
        # Имя сервиса в каждой записи — то же OTEL_SERVICE_NAME, что у
        # трассировки: логи, трейсы и ошибки должны называть сервис одинаково.
        static_fields={'service': settings.OTEL_SERVICE_NAME},
        logger_levels={
            'uvicorn': settings.LOG_LEVEL,
            'uvicorn.error': settings.LOG_LEVEL,
            'uvicorn.access': settings.LOG_LEVEL,
            # SQLAlchemy на INFO печатает каждый запрос и его параметры.
            'sqlalchemy.engine': 'WARNING',
        },
    )


LOGGING = build_config()


def setup_logging() -> None:
    _setup_logging(LOGGING)
