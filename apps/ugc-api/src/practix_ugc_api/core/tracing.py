"""Инициализация OpenTelemetry-трассировки сервиса UGC.

Реализация — в ``practix_core.tracing``; здесь остаются только параметры
сервиса. Сэмплирование не включается (``sampler_ratio`` не передаётся): оно
нужно ETL с десятками тысяч сообщений в секунду, а здесь поток запросов
сопоставим с Auth, и полная трассировка дешевле потери контекста инцидента.
"""

from practix_core.tracing import init_tracer_provider as _init_tracer_provider
from practix_core.tracing import instrument_fastapi
from practix_ugc_api.core.config import settings


def init_tracer_provider() -> None:
    """Идемпотентная настройка провайдера трассировки (один раз на воркер)."""
    _init_tracer_provider(
        enabled=settings.OTEL_ENABLED,
        service_name=settings.OTEL_SERVICE_NAME,
        endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT,
    )


def instrument_app(app) -> None:
    """Подключает FastAPI-инструментацию; ``/health/*`` исключён как трафик инфраструктуры."""
    instrument_fastapi(app, enabled=settings.OTEL_ENABLED, excluded_urls='health.*')
