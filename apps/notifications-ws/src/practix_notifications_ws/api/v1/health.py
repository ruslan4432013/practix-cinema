"""Пробы живости и готовности.

``/health/live`` не трогает зависимостей: перезапуск контейнера не чинит лежащий
Redis, зато превращает частичную недоступность в лавину рестартов — и в лавину
разорванных websocket-соединений заодно.

``/health/ready`` различает две беды, и ни одна из них не 503:

* **брокер недоступен** — push'ей нет, но открытые сокеты живы, а клиент по
  статусу ``degraded`` уходит догонять ленту. 503 здесь вывел бы из ротации все
  реплики сразу и оборвал бы все соединения — ровно то, чего деградация должна
  избежать;
* **Redis недоступен** — новые подключения невозможны (ticket негде выдать и
  нечем погасить), но уже открытые обслуживаются.

503 остаётся на случай, когда не работает НИЧТО: и брокера нет, и Redis нет —
тогда шлюз действительно бесполезен, и трафик лучше не слать. Трактовка
``ok / degraded / 503 только когда не работает ничто`` взята у коллектора и
UGC API, а не придумана третья.
"""

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, Field

from practix_notifications_ws.core import redis as redis_db
from practix_notifications_ws.core.config import settings

# Импортируется МОДУЛЬ, а не имена: `hub` пересоздаётся в lifespan, и
# `from … import hub` навсегда запомнил бы None, каким он был на импорте.
from practix_notifications_ws.services import providers

router = APIRouter()


class LivenessResponse(BaseModel):
    status: str = Field(description='Всегда "ok", пока процесс обрабатывает запросы')


class ReadinessResponse(BaseModel):
    status: str = Field(description='"ok" | "degraded" | "unavailable"')
    broker_connected: bool = Field(description='false означает, что push’и не доезжают и клиенту нужна лента')
    redis_connected: bool = Field(description='false означает, что новые подключения невозможны')
    connections: int = Field(description='Открытых websocket-соединений в этом процессе')
    connections_limit: int = Field(default=0, description='Потолок соединений на процесс')
    pollers: int = Field(default=0, description='Ждущих long-poll-запросов; бюджет соединений не расходуют')
    pollers_limit: int = Field(default=0, description='Свой потолок поллеров на процесс, отдельный от соединений')


@router.get('/live', response_model=LivenessResponse, summary='Проба живости')
async def liveness() -> LivenessResponse:
    return LivenessResponse(status='ok')


@router.get(
    '/ready',
    response_model=ReadinessResponse,
    summary='Проба готовности',
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {'description': 'Недоступны и брокер, и Redis'}},
)
async def readiness(response: Response) -> ReadinessResponse:
    consumer = providers.get_consumer()
    broker_connected = consumer is not None and consumer.connected
    redis_connected = await redis_db.reachable(await redis_db.get_client())
    connections = providers.hub.total if providers.hub is not None else 0
    # Отдельным числом: поллеры не расходуют бюджет соединений, но их всплеск —
    # признак того, что сокеты у клиентов не держатся. Рядом отдаются потолки:
    # иначе «1900 поллеров» ничего не говорит без заглядывания в .env. Насыщение
    # НЕ переводит шлюз в degraded — отказ отдельным клиентам это штатная защита,
    # а не поломка шлюза.
    pollers = providers.hub.pollers if providers.hub is not None else 0

    if not broker_connected and not redis_connected:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        state = 'unavailable'
    elif broker_connected and redis_connected:
        state = 'ok'
    else:
        state = 'degraded'

    return ReadinessResponse(
        status=state,
        broker_connected=broker_connected,
        redis_connected=redis_connected,
        connections=connections,
        connections_limit=settings.NOTIFY_WS_MAX_CONNECTIONS,
        pollers=pollers,
        pollers_limit=settings.NOTIFY_WS_MAX_POLLERS,
    )
