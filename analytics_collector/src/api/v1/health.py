"""Health-пробы и экспорт метрик.

Две пробы разделены сознательно, потому что отвечают на разные вопросы.

``/health/live`` — «жив ли процесс». Отвечает 200 всегда, пока приложение
способно обрабатывать запросы. Оркестратор по этой пробе перезапускает
контейнер, и завязывать её на состояние Kafka нельзя: перезапуск процесса
никак не чинит упавший брокер, зато превращает частичную деградацию в полный
отказ и лавину рестартов.

``/health/ready`` — «стоит ли слать сюда трафик». Отражает состояние продюсера
Kafka, но **тоже отвечает 200 при деградации**: пока работает буфер, сервис
принимает события без потерь, и выводить его из балансировки не нужно. 503
возвращается только когда недоступны и Kafka, и буфер — тогда события
действительно теряются, и лучше отдать трафик другому инстансу.
"""

from fastapi import APIRouter, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from core import metrics
from core.config import settings
from services.providers import get_broker, get_fallback_buffer

router = APIRouter()
# Метрики монтируются в корень (`/metrics`), а не под `/health`, — это путь,
# который сборщики Prometheus ожидают по умолчанию.
metrics_router = APIRouter()


class LivenessResponse(BaseModel):
    status: str = Field(description='Всегда "ok", пока процесс обрабатывает запросы')


class ReadinessResponse(BaseModel):
    status: str = Field(description='"ok" — штатная работа, "degraded" — события идут в буфер')
    broker_connected: bool = Field(description='Подключён ли продюсер к Kafka')
    fallback_enabled: bool = Field(description='Включён ли буфер деградации')
    buffered_events: int = Field(description='Число событий, ожидающих дренажа')


@router.get(
    '/live',
    response_model=LivenessResponse,
    summary='Проба живости',
    description='Возвращает 200, пока процесс способен обрабатывать запросы. От состояния Kafka не зависит.',
)
async def liveness() -> LivenessResponse:
    return LivenessResponse(status='ok')


@router.get(
    '/ready',
    response_model=ReadinessResponse,
    summary='Проба готовности',
    description=(
        'Отражает состояние продюсера Kafka. Возвращает 200 и при деградации, пока события '
        'принимаются в буфер без потерь; 503 — только когда недоступны и Kafka, и буфер.'
    ),
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {'description': 'События теряются'}},
)
async def readiness(response: Response) -> ReadinessResponse:
    broker = get_broker()
    buffer = get_fallback_buffer()

    broker_connected = bool(broker and broker.is_healthy)
    buffered = await buffer.size() if buffer else 0
    metrics.fallback_buffer_size.set(buffered)

    fallback_available = settings.UGC_FALLBACK_ENABLED and buffer is not None
    if not broker_connected and not fallback_available:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(
        status='ok' if broker_connected else 'degraded',
        broker_connected=broker_connected,
        fallback_enabled=fallback_available,
        buffered_events=buffered,
    )


@metrics_router.get(
    '/metrics',
    summary='Метрики Prometheus',
    description='Экспорт метрик в текстовом формате Prometheus.',
    include_in_schema=False,
)
async def prometheus_metrics() -> Response:
    if not settings.UGC_METRICS_ENABLED:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    return Response(content=generate_latest(metrics.build_scrape_registry()), media_type=CONTENT_TYPE_LATEST)
