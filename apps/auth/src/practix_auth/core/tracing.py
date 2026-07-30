"""Инициализация OpenTelemetry-трассировки Auth-сервиса.

Auth запускается одним процессом, поэтому провайдер трассировки поднимается на
уровне модуля (в отличие от ``rest``/коллектора, где вызов идёт из ``lifespan``
по разу на воркер).

Реализация переехала в ``practix_core.tracing``. С ``rest/core/tracing.py`` этот
модуль расходился ТОЛЬКО регистром имён настроек и отсутствием
httpx-инструментации: исходящих HTTP-вызовов у сервиса нет, поэтому список
инструментируемых библиотек пуст.
"""

from practix_auth.core.config import settings
from practix_core.tracing import init_tracer_provider as _init_tracer_provider
from practix_core.tracing import instrument_fastapi


def init_tracer_provider() -> None:
    """Идемпотентная настройка провайдера трассировки."""
    _init_tracer_provider(
        enabled=settings.OTEL_ENABLED,
        service_name=settings.OTEL_SERVICE_NAME,
        endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT,
    )


def instrument_app(app) -> None:
    """Подключает FastAPI-инструментацию (серверные span'ы)."""
    instrument_fastapi(app, enabled=settings.OTEL_ENABLED)
