"""Инициализация OpenTelemetry-трассировки и экспорт спанов в Jaeger (OTLP/HTTP).

Auth-сервис запускается одним процессом, поэтому провайдер трассировки
поднимается на уровне модуля. Исходящих HTTP-вызовов у сервиса нет, поэтому
httpx-инструментация не требуется.
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
    """Идемпотентная настройка провайдера трассировки."""
    global _INITIALIZED
    if _INITIALIZED or not settings.OTEL_ENABLED:
        return
    resource = Resource.create({SERVICE_NAME: settings.OTEL_SERVICE_NAME})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=f'{settings.OTEL_EXPORTER_OTLP_ENDPOINT.rstrip("/")}/v1/traces')
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _INITIALIZED = True
    logger.info('OpenTelemetry tracing initialized for %s', settings.OTEL_SERVICE_NAME)


def instrument_app(app) -> None:
    """Подключает FastAPI-инструментацию (серверные span'ы)."""
    if not settings.OTEL_ENABLED:
        return
    FastAPIInstrumentor().instrument_app(app)
