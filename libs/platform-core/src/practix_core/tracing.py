"""Инициализация OpenTelemetry и экспорт спанов в Jaeger (OTLP/HTTP).

Модуль существовал в четырёх копиях с побайтово одинаковым флагом идемпотентности
и одинаковым пятистрочным поднятием провайдера. Между `rest` и `auth` расхождение
сводилось к РЕГИСТРУ имён настроек (``settings.otel_enabled`` против
``settings.OTEL_ENABLED``) — то есть к нулю смысловых отличий.

Настоящие отличия сервисов выражены здесь аргументами:

* ``instrumentors`` — какие библиотеки патчить. `rest` патчит ``httpx`` (иначе
  исходящие вызовы в Auth не получат ``traceparent``), коллектор и ETL —
  ``aiokafka`` (иначе трейс обрывается на границе брокера), `auth` — ничего.
* ``sampler_ratio`` — только у ETL. ``None`` означает «трассировать всё» и
  сохраняет поведение остальных трёх сервисов.
* FastAPI-инструментация вынесена в отдельную функцию: у ETL нет HTTP-API.

## Почему сэмплирование у ETL обязательно

Коллектор трассирует HTTP-запросы, ETL — поток в десятки тысяч сообщений в
секунду. Спан на каждое сообщение сам станет проблемой: экспорт займёт больше
ресурсов, чем полезная работа. ``ParentBased`` поверх ratio обязателен именно в
такой комбинации: если коллектор уже решил трассировать запрос, ETL обязан
продолжить трейс, а не бросать монетку заново — иначе трейс получился бы с дырой
посередине. Своё решение принимается только для сообщений без родительского
контекста.

## ``_INITIALIZED`` теперь общий на процесс

Раньше флаг был свой у каждого сервиса. Это имеет значение только если два
приложения окажутся в одном процессе. Функциональные наборы действительно
поднимают приложения in-process через ``ASGITransport``, но по одному приложению
на набор, поэтому сегодня разницы нет. Если когда-нибудь понадобится поднять два
приложения рядом — сбрасывать флаг следует через ``reset_for_testing()``.
"""

import logging
from collections.abc import Callable, Mapping, Sequence

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

logger = logging.getLogger(__name__)

_INITIALIZED = False


def reset_for_testing() -> None:
    """Сбрасывает флаг идемпотентности. Только для тестов."""
    global _INITIALIZED
    _INITIALIZED = False


def instrument_httpx() -> None:
    """Патчит все ``httpx``-клиенты, чтобы исходящие запросы несли ``traceparent``.

    Инструментация глобальная, поэтому её действие распространяется и на клиент,
    созданный в ``lifespan`` до вызова этой функции.
    """
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    HTTPXClientInstrumentor().instrument()


def instrument_aiokafka() -> None:
    """Патчит ``aiokafka``: спаны отправки и контекст трейса в заголовках сообщения.

    Благодаря заголовкам трейс не обрывается на границе брокера: спан консьюмера
    связывается со спаном HTTP-запроса, в котором событие было принято, даже если
    между ними прошли часы.

    Отсутствие пакета НЕ является ошибкой: сервис обязан работать без
    инструментации, потеряв лишь связность трейсов через брокер.
    """
    try:
        from opentelemetry.instrumentation.aiokafka import AIOKafkaInstrumentor

        AIOKafkaInstrumentor().instrument()
    except Exception as exc:  # noqa: BLE001 — отсутствие инструментации не критично
        logger.info('aiokafka instrumentation is not enabled: %s', exc)


def init_tracer_provider(
    *,
    enabled: bool,
    service_name: str,
    endpoint: str,
    sampler_ratio: float | None = None,
    instrumentors: Sequence[Callable[[], None]] = (),
    resource_attributes: Mapping[str, str] | None = None,
) -> None:
    """Идемпотентная настройка провайдера трассировки.

    :param sampler_ratio: доля трассируемых корневых трейсов. ``None`` —
        сэмплирование не настраивается (поведение по умолчанию SDK: трассируется
        всё). Значение задаёт только ETL.
    :param instrumentors: функции инструментации, вызываемые после установки
        провайдера — порядок важен, инструментация должна видеть провайдер.
    """
    global _INITIALIZED
    if _INITIALIZED or not enabled:
        return

    attributes: dict[str, str] = {SERVICE_NAME: service_name}
    if resource_attributes:
        attributes.update(resource_attributes)
    resource = Resource.create(attributes)

    if sampler_ratio is None:
        provider = TracerProvider(resource=resource)
    else:
        provider = TracerProvider(resource=resource, sampler=ParentBased(root=TraceIdRatioBased(sampler_ratio)))

    exporter = OTLPSpanExporter(endpoint=f'{endpoint.rstrip("/")}/v1/traces')
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    for instrument in instrumentors:
        instrument()

    _INITIALIZED = True
    # Текст сообщения сохранён от rest/auth: по нему могут быть настроены
    # оповещения и поиск в логах. Детали ушли в extra=.
    logger.info(
        'OpenTelemetry tracing initialized for %s',
        service_name,
        extra={'service': service_name, 'sampler_ratio': sampler_ratio},
    )


def instrument_fastapi(app, *, enabled: bool = True, excluded_urls: str | None = None) -> None:
    """Подключает FastAPI-инструментацию (серверные span'ы).

    :param excluded_urls: маршруты, которые не трассируются. Коллектор исключает
        ``health.*,metrics`` — это трафик инфраструктуры, который иначе занял бы
        собой почти весь объём трассировки.
    """
    if not enabled:
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    if excluded_urls:
        FastAPIInstrumentor().instrument_app(app, excluded_urls=excluded_urls)
    else:
        FastAPIInstrumentor().instrument_app(app)


def get_tracer(name: str) -> trace.Tracer:
    """Трейсер для ручных спанов.

    Работает и без инициализации провайдера: OpenTelemetry возвращает no-op
    реализацию, поэтому код вызывающей стороны не обрастает проверками флага.
    """
    return trace.get_tracer(name)
