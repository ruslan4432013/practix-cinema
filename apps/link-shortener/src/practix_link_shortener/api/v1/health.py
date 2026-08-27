"""Health-пробы.

``/health/live`` — «жив ли процесс». От базы не зависит: перезапуск контейнера
не чинит упавший PostgreSQL, зато превращает частичную недоступность в лавину
рестартов.

``/health/ready`` — «стоит ли слать сюда трафик». Зависимость одна: PostgreSQL.
Ни Redis, ни денилиста у сервиса нет — переход по ссылке анонимен.

Доступность Auth в готовность НЕ входит, и это решение. Обычные ссылки
(``kind='redirect'``) без Auth работают, а ссылки подтверждения отдают честные
503 с ``Retry-After``. Вывести реплику из ротации из-за чужого сервиса значило бы
уронить и работающую половину.
"""

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from practix_link_shortener.db.postgres import get_session

router = APIRouter()


class LivenessResponse(BaseModel):
    status: str = Field(description='Всегда "ok", пока процесс обрабатывает запросы')


class ReadinessResponse(BaseModel):
    status: str = Field(description='"ok" — база доступна; "unavailable" — нет')
    database_connected: bool


@router.get(
    '/live',
    response_model=LivenessResponse,
    summary='Проба живости',
    description='Возвращает 200, пока процесс способен обрабатывать запросы. От состояния базы не зависит.',
)
async def liveness() -> LivenessResponse:
    return LivenessResponse(status='ok')


@router.get(
    '/ready',
    response_model=ReadinessResponse,
    summary='Проба готовности',
    description='503, если недоступна база: без неё нельзя ни завести ссылку, ни разрешить код.',
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {'description': 'База недоступна'}},
)
async def readiness(response: Response, db: AsyncSession = Depends(get_session)) -> ReadinessResponse:
    try:
        await db.execute(text('SELECT 1'))
    except Exception:  # noqa: BLE001 — любая ошибка драйвера здесь означает «не готов»
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(status='unavailable', database_connected=False)

    return ReadinessResponse(status='ok', database_connected=True)
