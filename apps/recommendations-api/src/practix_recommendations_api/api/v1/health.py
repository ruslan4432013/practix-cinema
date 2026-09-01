"""Health-пробы и экспорт метрик.

``/health/live`` — «жив ли процесс». От витрины не зависит: перезапуск
контейнера не чинит упавший PostgreSQL, зато превращает частичную недоступность
в лавину рестартов.

``/health/ready`` — «стоит ли слать сюда трафик», и здесь ответ тоньше, чем у
соседей. Отсутствие горячего слоя или пустая витрина — это ``degraded`` и
всё-таки **200**: сервис продолжает отдавать блок, просто медленнее или
популярным. Выводить реплику из ротации незачем — другая реплика ответит ровно
так же. 503 остаётся за случаем, когда недоступна база: тогда всё, что мы можем
отдать, — популярное из памяти процесса, и лучше отдать трафик соседу, который
может больше.

Проба ЕЩЁ И ОБНОВЛЯЕТ МЕТРИКУ СВЕЖЕСТИ. Возраст витрины (F0.4) читается из
базы, а горячий путь запроса туда за ним не ходит — иначе кэш указателя не
экономил бы ничего. Раз в десять секунд для суточного цикла обучения — с
запасом.
"""

from fastapi import APIRouter, Depends, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from practix_core.health import reachable
from practix_recommendations_api.core.config import settings
from practix_recommendations_api.db.postgres import get_session
from practix_recommendations_api.db.redis import get_auth_redis, get_recs_redis
from practix_recommendations_api.services import metrics
from practix_recommendations_api.services.shelf import ShelfReader

router = APIRouter()
# Метрики монтируются в корень (`/metrics`), а не под `/health`, — это путь,
# который сборщики Prometheus ожидают по умолчанию.
metrics_router = APIRouter()


class LivenessResponse(BaseModel):
    status: str = Field(description='Всегда "ok", пока процесс обрабатывает запросы')


class ReadinessResponse(BaseModel):
    status: str = Field(description='"ok" — всё на месте; "degraded" — отдаём популярное; "unavailable" — нет базы')
    shelf_connected: bool = Field(description='Доступна ли витрина в PostgreSQL')
    cache_connected: bool = Field(description='Доступен ли горячий слой Redis')
    auth_denylist_connected: bool = Field(description='Доступен ли денилист отозванных токенов в Redis Auth')
    shelf_version: int | None = Field(description='Версия витрины, из которой идёт выдача; null — обучения не было')
    shelf_age_seconds: float | None = Field(description='Возраст витрины; алерт по SLO — 36 часов')


@router.get(
    '/live',
    response_model=LivenessResponse,
    summary='Проба живости',
    description='Возвращает 200, пока процесс способен обрабатывать запросы. От состояния витрины не зависит.',
)
async def liveness() -> LivenessResponse:
    return LivenessResponse(status='ok')


@router.get(
    '/ready',
    response_model=ReadinessResponse,
    summary='Проба готовности',
    description=(
        '200 при штатной работе и при деградации (нет кэша, витрина пуста — блок всё равно отрисуется). '
        '503 только когда недоступна база: тогда отдавать можно лишь популярное из памяти процесса.'
    ),
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {'description': 'Витрина недоступна'}},
)
async def readiness(response: Response, db: AsyncSession = Depends(get_session)) -> ReadinessResponse:
    cache_connected = await reachable(await get_recs_redis(), settings.RECS_REDIS_TIMEOUT)
    denylist_connected = await reachable(await get_auth_redis(), settings.RECS_AUTH_REDIS_TIMEOUT)

    try:
        state = await ShelfReader(db).get_state()
    except Exception:  # noqa: BLE001 — любая ошибка драйвера здесь означает «витрина недоступна»
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(
            status='unavailable',
            shelf_connected=False,
            cache_connected=cache_connected,
            auth_denylist_connected=denylist_connected,
            shelf_version=None,
            shelf_age_seconds=None,
        )

    age = state.age_seconds
    if age is not None:
        metrics.shelf_age.set(age)
    if state.version is not None:
        metrics.shelf_version.set(state.version)

    # Пустая витрина и отсутствие кэша — деградация, но не повод убирать реплику
    # из ротации: соседняя реплика отдаст ровно то же самое.
    healthy = cache_connected and state.version is not None
    return ReadinessResponse(
        status='ok' if healthy else 'degraded',
        shelf_connected=True,
        cache_connected=cache_connected,
        auth_denylist_connected=denylist_connected,
        shelf_version=state.version,
        shelf_age_seconds=age,
    )


@metrics_router.get(
    '/metrics',
    summary='Метрики Prometheus',
    description='Экспорт метрик в текстовом формате Prometheus.',
    include_in_schema=False,
)
async def prometheus_metrics() -> Response:
    if not settings.RECS_METRICS_ENABLED:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    return Response(content=generate_latest(metrics.build_scrape_registry()), media_type=CONTENT_TYPE_LATEST)
