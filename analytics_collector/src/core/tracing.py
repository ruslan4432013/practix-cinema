"""Инициализация OpenTelemetry и экспорт спанов в Jaeger (OTLP/HTTP).

Повторяет подход ``rest/core/tracing.py``: провайдер поднимается по одному разу
на каждый uvicorn-воркер (вызов из ``lifespan``), спаны уходят батчами по
OTLP/HTTP на порт 4318 Jaeger.

Специфика этого сервиса — инструментация ``aiokafka``: она добавляет спаны
отправки сообщений и, что важнее, кладёт в заголовки сообщения контекст трейса.
Благодаря этому трейс не обрывается на границе брокера: спан консьюмера,
обработавшего событие, связывается со спаном HTTP-запроса, в котором событие
было принято, — даже если между ними прошли часы.
"""

import logging

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from core.config import settings

logger = logging.getLogger(__name__)

_INITIALIZED = False


def init_tracer_provider() -> None:
    """Идемпотентная настройка провайдера трассировки (один раз на воркер)."""
    global _INITIALIZED
    if _INITIALIZED or not settings.OTEL_ENABLED:
        return

    resource = Resource.create({SERVICE_NAME: settings.OTEL_SERVICE_NAME})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=f'{settings.OTEL_EXPORTER_OTLP_ENDPOINT.rstrip("/")}/v1/traces')
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    # Инструментация aiokafka опциональна: если пакет не установлен, сервис
    # обязан работать без неё, потеряв лишь связность трейсов через брокер.
    try:
        from opentelemetry.instrumentation.aiokafka import AIOKafkaInstrumentor

        AIOKafkaInstrumentor().instrument()
    except Exception as exc:  # noqa: BLE001 — отсутствие инструментации не критично
        logger.info('aiokafka instrumentation is not enabled: %s', exc)

    _INITIALIZED = True
    logger.info('OpenTelemetry tracing initialized for %s', settings.OTEL_SERVICE_NAME)


def instrument_app(app) -> None:
    """Подключает FastAPI-инструментацию (серверные span'ы).

    ``/health/*`` и ``/metrics`` исключены: это трафик инфраструктуры, который
    иначе занял бы собой почти весь объём трассировки.
    """
    if not settings.OTEL_ENABLED:
        return
    FastAPIInstrumentor().instrument_app(app, excluded_urls='health.*,metrics')
