"""Инициализация OpenTelemetry и экспорт спанов в Jaeger (OTLP/HTTP).

ЗАЧЕМ ЗДЕСЬ. Коллектор проставляет ``traceparent`` в заголовках сообщений
Kafka, но на стороне ETL инструментации не было, и цепочка «браузер →
коллектор → Kafka» обрывалась ровно там, где начинается интересное: на пути
события в хранилище. Вопрос «почему это событие доехало до ClickHouse через
двадцать минут» без спана вставки не имеет ответа.

ДВА ОТЛИЧИЯ ОТ ``analytics_collector/src/core/tracing.py``:

1. **Сэмплирование обязательно.** Коллектор трассирует HTTP-запросы, ETL —
   поток в десятки тысяч сообщений в секунду. Спан на каждое сообщение сам
   станет проблемой: экспорт займёт больше ресурсов, чем полезная работа.
   ``TraceIdRatioBased`` принимает решение по ``trace_id``, поэтому оно
   согласовано с решением коллектора — трейс либо целиком попадает в выборку,
   либо целиком нет, а не рвётся посередине.
2. **Нет FastAPI-инструментации.** У сервиса нет HTTP-API: единственная ручка
   ``/metrics`` поднимается голым сервером и трассировать её незачем.

Сервис однопроцессный, поэтому провайдер поднимается один раз при старте (в
коллекторе — по разу на каждый uvicorn-воркер).
"""

import logging

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from core.config import settings

logger = logging.getLogger(__name__)

_INITIALIZED = False


def init_tracer_provider() -> None:
    """Идемпотентная настройка провайдера трассировки."""
    global _INITIALIZED
    if _INITIALIZED or not settings.OTEL_ENABLED:
        return

    # ParentBased поверх ratio: если коллектор уже решил трассировать запрос,
    # мы обязаны продолжить трейс, а не бросать монетку заново — иначе трейс
    # получился бы с дырой посередине. Своё решение принимается только для
    # сообщений без родительского контекста.
    sampler = ParentBased(root=TraceIdRatioBased(settings.OTEL_TRACES_SAMPLER_RATIO))

    resource = Resource.create({SERVICE_NAME: settings.OTEL_SERVICE_NAME})
    provider = TracerProvider(resource=resource, sampler=sampler)
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
    logger.info(
        'OpenTelemetry tracing initialized',
        extra={'service': settings.OTEL_SERVICE_NAME, 'sampler_ratio': settings.OTEL_TRACES_SAMPLER_RATIO},
    )


def get_tracer() -> trace.Tracer:
    """Трейсер для ручных спанов (вставка пачки в ClickHouse).

    Работает и без инициализации провайдера: OpenTelemetry возвращает no-op
    реализацию, поэтому код вызывающей стороны не обрастает проверками флага.
    """
    return trace.get_tracer('etl.clickhouse')
