"""Инициализация OpenTelemetry-трассировки Movies API.

Провайдер трассировки поднимается по одному разу на каждый uvicorn-воркер
(вызов из ``lifespan``). Экспортёр отправляет спаны в Jaeger по OTLP/HTTP.

Реализация переехала в ``practix_core.tracing``: модуль существовал в четырёх
копиях, а с ``auth/src/core/tracing.py`` расходился ТОЛЬКО регистром имён
настроек. Здесь остались значения и набор инструментируемых библиотек.
"""

from practix_core.tracing import init_tracer_provider as _init_tracer_provider
from practix_core.tracing import instrument_fastapi, instrument_httpx
from practix_movies_api.core.config import settings


def init_tracer_provider() -> None:
    """Идемпотентная настройка провайдера трассировки (один раз на воркер)."""
    _init_tracer_provider(
        enabled=settings.otel_enabled,
        service_name=settings.otel_service_name,
        endpoint=settings.otel_exporter_otlp_endpoint,
        # httpx патчится, чтобы traceparent автоматически прокидывался в
        # Auth-сервис: без этого трейс обрывается на межсервисном вызове.
        instrumentors=(instrument_httpx,),
    )


def instrument_app(app) -> None:
    """Подключает FastAPI-инструментацию (серверные span'ы)."""
    instrument_fastapi(app, enabled=settings.otel_enabled)
