"""Health-пробы.

Две пробы отвечают на разные вопросы, поэтому и различаются.

``/health/live`` — «жив ли процесс». Не зависит ни от базы, ни от Redis:
перезапуск контейнера не чинит упавшую зависимость, зато превращает частичную
недоступность в лавину рестартов.

``/health/ready`` — «стоит ли слать сюда трафик». Зависимостей у ответа две, и
вес у них разный.

* **PostgreSQL** — без него сервис не может ни прочитать, ни сохранить. Это
  честные 503.
* **Redis Auth-сервиса** — из него читается денилист отозванных токенов, и
  политика ``on_error='deny'`` (см. ``main.py``) означает, что при его
  недоступности отозванным считается ЛЮБОЙ токен: 401 на все оценки, закладки,
  рецензии и голоса. Молчать об этом в пробе нельзя — снаружи под выглядел бы
  здоровым, пока каждая запись отклоняется, — поэтому состояние денилиста
  отдаётся полем ``auth_denylist_connected``, а статус становится ``degraded``.

Почему падение Redis — это НЕ 503. Экземпляр один на все реплики сервиса, значит
503 вывел бы из ротации сразу все поды и превратил «401 на запись» в «503 на
всё», убив заодно публичные чтения (рейтинг фильма, список рецензий), которые в
этот момент исправны: токен там не проверяется. Отказ части контракта хуже
маскировать, чем отказ всего, но лечить его отключением исправной части ещё
хуже. Разделение «ok / degraded / 503 только когда не работает ничто» уже
применено в коллекторе — берём его, а не заводим третью трактовку.
"""

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from practix_ugc_api.db.postgres import get_session
from practix_ugc_api.db.redis import denylist_reachable, get_auth_redis

router = APIRouter()


class LivenessResponse(BaseModel):
    status: str = Field(description='Всегда "ok", пока процесс обрабатывает запросы')


class ReadinessResponse(BaseModel):
    status: str = Field(
        description=(
            '"ok" — доступны база и денилист; "degraded" — база доступна, но денилист нет, '
            'и авторизованные запросы отклоняются; "unavailable" — недоступна база'
        )
    )
    database_connected: bool
    auth_denylist_connected: bool = Field(description='Redis Auth-сервиса. false означает 401 на все запросы с токеном')


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
    description=(
        '503, если недоступна база: без неё сервис не может ни прочитать, ни сохранить.\n\n'
        'Недоступность Redis Auth-сервиса — это 200 со статусом `degraded`: записи в этом '
        'состоянии отклоняются с 401 (денилист читается с политикой `deny`), но публичные '
        'чтения работают, а экземпляр Redis общий для всех реплик — 503 вывел бы из ротации '
        'сразу все и отключил бы исправное чтение.'
    ),
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {'description': 'База недоступна'}},
)
async def readiness(
    response: Response,
    db: AsyncSession = Depends(get_session),
    auth_redis: Redis | None = Depends(get_auth_redis),
) -> ReadinessResponse:
    # Проверка общая со стартом сервиса (см. lifespan в main.py), но логировать
    # её здесь не нужно: загрузчик денилиста уже пишет WARNING на каждый
    # отвергнутый запрос, проба добавила бы только шум раз в интервал опроса.
    denylist_connected = await denylist_reachable(auth_redis)

    try:
        await db.execute(text('SELECT 1'))
    except Exception:  # noqa: BLE001 — любая ошибка драйвера здесь означает «не готов»
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(
            status='unavailable', database_connected=False, auth_denylist_connected=denylist_connected
        )

    return ReadinessResponse(
        status='ok' if denylist_connected else 'degraded',
        database_connected=True,
        auth_denylist_connected=denylist_connected,
    )
