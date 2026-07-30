"""Инициализация OpenTelemetry-трассировки и экспорт спанов в Jaeger (OTLP/HTTP).

Провайдер трассировки поднимается по одному разу на каждый uvicorn-воркер
(вызов из ``lifespan``). Экспортёр отправляет спаны в Jaeger по OTLP/HTTP.
``HTTPXClientInstrumentor`` глобально патчит все ``httpx``-клиенты, поэтому
исходящие запросы в Auth-сервис автоматически получают заголовок
``traceparent`` (связывание трейсов между сервисами).
"""

import logging

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from core.config import settings

logger = logging.getLogger(__name__)

_INITIALIZED = False


def init_tracer_provider() -> None:
    """Идемпотентная настройка провайдера трассировки (один раз на воркер)."""
    global _INITIALIZED
    if _INITIALIZED or not settings.otel_enabled:
        return
    resource = Resource.create({SERVICE_NAME: settings.otel_service_name})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=f'{settings.otel_exporter_otlp_endpoint.rstrip("/")}/v1/traces')
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    # Патчим исходящие httpx-запросы (в т.ч. синглтон-клиент из lifespan),
    # чтобы traceparent автоматически прокидывался в Auth-сервис.
    HTTPXClientInstrumentor().instrument()
    _INITIALIZED = True
    logger.info('OpenTelemetry tracing initialized for %s', settings.otel_service_name)


def instrument_app(app) -> None:
    """Подключает FastAPI-инструментацию (серверные span'ы)."""
    if not settings.otel_enabled:
        return
    FastAPIInstrumentor().instrument_app(app)
