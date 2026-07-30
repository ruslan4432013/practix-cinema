"""Инициализация OpenTelemetry-трассировки ETL ClickHouse.

ЗАЧЕМ ЗДЕСЬ. Коллектор проставляет ``traceparent`` в заголовках сообщений Kafka,
но на стороне ETL инструментации не было, и цепочка «браузер → коллектор →
Kafka» обрывалась ровно там, где начинается интересное: на пути события в
хранилище. Вопрос «почему это событие доехало до ClickHouse через двадцать минут»
без спана вставки не имеет ответа.

ДВА ОТЛИЧИЯ ОТ СИБЛИНГОВ, теперь выраженные аргументами, а не копией модуля:

1. **Сэмплирование обязательно.** Коллектор трассирует HTTP-запросы, ETL — поток
   в десятки тысяч сообщений в секунду. Спан на каждое сообщение сам станет
   проблемой: экспорт займёт больше ресурсов, чем полезная работа. Остальные три
   сервиса передают ``sampler_ratio=None`` (трассируется всё), и по умолчанию в
   библиотеке стоит именно ``None`` — чтобы нельзя было случайно включить
   сэмплирование там, где оно не нужно.
2. **Нет FastAPI-инструментации.** У сервиса нет HTTP-API: единственная ручка
   ``/metrics`` поднимается голым сервером и трассировать её незачем — поэтому
   ``instrument_fastapi`` здесь просто не вызывается.

Сервис однопроцессный, поэтому провайдер поднимается один раз при старте.
"""

from opentelemetry import trace

from practix_core.tracing import get_tracer as _get_tracer
from practix_core.tracing import init_tracer_provider as _init_tracer_provider
from practix_core.tracing import instrument_aiokafka
from practix_etl_clickhouse.core.config import settings


def init_tracer_provider() -> None:
    """Идемпотентная настройка провайдера трассировки."""
    _init_tracer_provider(
        enabled=settings.OTEL_ENABLED,
        service_name=settings.OTEL_SERVICE_NAME,
        endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT,
        # ParentBased поверх ratio: если коллектор уже решил трассировать запрос,
        # мы обязаны продолжить трейс, а не бросать монетку заново — иначе трейс
        # получился бы с дырой посередине.
        sampler_ratio=settings.OTEL_TRACES_SAMPLER_RATIO,
        instrumentors=(instrument_aiokafka,),
    )


def get_tracer() -> trace.Tracer:
    """Трейсер для ручных спанов (вставка пачки в ClickHouse).

    Работает и без инициализации провайдера: OpenTelemetry возвращает no-op
    реализацию, поэтому код вызывающей стороны не обрастает проверками флага.
    """
    return _get_tracer('etl.clickhouse')
