"""Инициализация OpenTelemetry-трассировки коллектора.

Провайдер поднимается по одному разу на каждый uvicorn-воркер (вызов из
``lifespan``), спаны уходят батчами по OTLP/HTTP на порт 4318 Jaeger.

Специфика этого сервиса — инструментация ``aiokafka``: она добавляет спаны
отправки сообщений и, что важнее, кладёт в заголовки сообщения контекст трейса.
Благодаря этому трейс не обрывается на границе брокера: спан консьюмера,
обработавшего событие, связывается со спаном HTTP-запроса, в котором событие было
принято, — даже если между ними прошли часы.

Реализация переехала в ``practix_core.tracing``; здесь остались только набор
инструментируемых библиотек и список неотслеживаемых маршрутов.
"""

from practix_analytics_collector.core.config import settings
from practix_core.tracing import init_tracer_provider as _init_tracer_provider
from practix_core.tracing import instrument_aiokafka, instrument_fastapi


def init_tracer_provider() -> None:
    """Идемпотентная настройка провайдера трассировки (один раз на воркер)."""
    _init_tracer_provider(
        enabled=settings.OTEL_ENABLED,
        service_name=settings.OTEL_SERVICE_NAME,
        endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT,
        instrumentors=(instrument_aiokafka,),
    )


def instrument_app(app) -> None:
    """Подключает FastAPI-инструментацию (серверные span'ы).

    ``/health/*`` и ``/metrics`` исключены: это трафик инфраструктуры, который
    иначе занял бы собой почти весь объём трассировки.
    """
    instrument_fastapi(app, enabled=settings.OTEL_ENABLED, excluded_urls='health.*,metrics')
