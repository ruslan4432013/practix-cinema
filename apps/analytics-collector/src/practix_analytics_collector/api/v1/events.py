"""Ingest-API пользовательских событий.

Все ручки отвечают ``202 Accepted``, а не ``201``: сервис принимает событие на
себя, но подтверждение записи от кворума реплик Kafka к этому моменту ещё не
получено. ``202`` — честное описание такой семантики.

Отдельные ручки на клики и просмотры страниц (а не одна универсальная) сделаны
намеренно: они дают строгую схему в OpenAPI, а браузерному коду не нужно
дублировать поле ``event_type`` в каждом запросе. Универсальные ``/custom`` и
``/batch`` используют дискриминацию по ``event_type``.
"""

import logging
from collections import Counter

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, Field

from practix_analytics_collector.api.v1.dependencies import get_principal, get_request_context
from practix_analytics_collector.core.config import settings
from practix_analytics_collector.core.rate_limit import Charge, RateLimiter
from practix_analytics_collector.db.redis import get_redis
from practix_analytics_collector.models.enums import DeliveryStatus
from practix_analytics_collector.models.requests import (
    BaseEventIn,
    BatchEventsIn,
    ClickEventIn,
    CustomEventIn,
    PageViewEventIn,
)
from practix_analytics_collector.services.event_service import EventService, IngestResult, Principal, RequestContext
from practix_analytics_collector.services.providers import get_event_service

logger = logging.getLogger(__name__)

router = APIRouter()


class EventAcceptedResponse(BaseModel):
    """Ответ на приём одного события."""

    event_id: str = Field(description='Идентификатор события (сгенерирован сервером, если не был задан)')
    status: DeliveryStatus = Field(description='Что сервис сделал с событием')


class BatchAcceptedResponse(BaseModel):
    """Ответ на приём пачки событий."""

    accepted: int = Field(description='Событий передано в Kafka')
    buffered: int = Field(description='Событий отложено в буфер деградации')
    duplicates: int = Field(description='Событий подавлено дедупликацией')
    dropped: int = Field(description='Событий потеряно (недоступны и Kafka, и буфер)')
    filtered: int = Field(default=0, description='Событий отброшено фильтром (трафик ботов)')
    results: list[EventAcceptedResponse] = Field(description='Результат по каждому событию пачки')


def _to_response(result: IngestResult) -> EventAcceptedResponse:
    return EventAcceptedResponse(event_id=str(result.event_id), status=result.status)


# Описание ответов повторяется во всех ручках, поэтому вынесено в константу.
_COMMON_RESPONSES = {
    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE: {'description': 'Тело запроса превышает допустимый размер'},
    status.HTTP_422_UNPROCESSABLE_ENTITY: {'description': 'Событие не прошло валидацию'},
    status.HTTP_429_TOO_MANY_REQUESTS: {'description': 'Превышен лимит частоты запросов'},
}


@router.post(
    '/click',
    response_model=EventAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary='Отправить событие клика',
    description=(
        'Регистрирует клик пользователя по элементу интерфейса: карточке фильма, трейлеру, '
        'категории, баннеру и т. п. Аутентификация опциональна — при наличии валидного '
        'Bearer-токена событие атрибутируется пользователю, иначе считается анонимным.'
    ),
    response_description='Событие принято к отправке',
    responses=_COMMON_RESPONSES,
)
async def track_click(
    event: ClickEventIn,
    principal: Principal = Depends(get_principal),
    request_context: RequestContext = Depends(get_request_context),
    service: EventService = Depends(get_event_service),
) -> EventAcceptedResponse:
    await enforce_ingest_limits(request_context, [event])
    return _to_response(await service.ingest(event, principal, request_context))


@router.post(
    '/page-view',
    response_model=EventAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary='Отправить событие просмотра страницы',
    description=(
        'Регистрирует просмотр страницы и время, проведённое на ней. Поле `duration_ms` '
        'обычно отправляется при уходе со страницы через `navigator.sendBeacon`.'
    ),
    response_description='Событие принято к отправке',
    responses=_COMMON_RESPONSES,
)
async def track_page_view(
    event: PageViewEventIn,
    principal: Principal = Depends(get_principal),
    request_context: RequestContext = Depends(get_request_context),
    service: EventService = Depends(get_event_service),
) -> EventAcceptedResponse:
    await enforce_ingest_limits(request_context, [event])
    return _to_response(await service.ingest(event, principal, request_context))


@router.post(
    '/custom',
    response_model=EventAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary='Отправить кастомное событие',
    description=(
        'Регистрирует одно из кастомных событий: смену качества видео '
        '(`video_quality_change`), просмотр до конца (`video_completed`) или применение '
        'фильтров поиска (`search_filter_used`). Конкретный тип задаётся полем `event_type`.'
    ),
    response_description='Событие принято к отправке',
    responses=_COMMON_RESPONSES,
)
async def track_custom(
    event: CustomEventIn = Body(),
    principal: Principal = Depends(get_principal),
    request_context: RequestContext = Depends(get_request_context),
    service: EventService = Depends(get_event_service),
) -> EventAcceptedResponse:
    await enforce_ingest_limits(request_context, [event])
    return _to_response(await service.ingest(event, principal, request_context))


@router.post(
    '/batch',
    response_model=BatchAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary='Отправить пачку событий',
    description=(
        'Принимает до `UGC_MAX_BATCH_SIZE` событий любых типов одним запросом. Основной '
        'сценарий — отправка накопленной очереди при уходе со страницы. Каждое событие '
        'пачки обрабатывается независимо, поэтому в ответе возвращается статус по каждому.\n\n'
        'Пачка расходует лимит частоты пропорционально числу событий: батч из 50 событий '
        'стоит столько же, сколько 50 отдельных запросов.'
    ),
    response_description='Пачка обработана',
    responses=_COMMON_RESPONSES,
)
async def track_batch(
    batch: BatchEventsIn,
    principal: Principal = Depends(get_principal),
    request_context: RequestContext = Depends(get_request_context),
    service: EventService = Depends(get_event_service),
) -> BatchAcceptedResponse:
    # Все списания лимита (вес пачки по IP, идентификатор браузера, сессия)
    # уходят в Redis одним pipeline — см. enforce_ingest_limits.
    await enforce_ingest_limits(request_context, list(batch.events))

    results = await service.ingest_batch(list(batch.events), principal, request_context)
    counts = dict.fromkeys(DeliveryStatus, 0)
    for result in results:
        counts[result.status] += 1

    return BatchAcceptedResponse(
        accepted=counts[DeliveryStatus.ACCEPTED],
        buffered=counts[DeliveryStatus.BUFFERED],
        duplicates=counts[DeliveryStatus.DUPLICATE],
        dropped=counts[DeliveryStatus.DROPPED],
        filtered=counts[DeliveryStatus.FILTERED],
        results=[_to_response(result) for result in results],
    )


async def enforce_ingest_limits(request_context: RequestContext, events: list[BaseEventIn]) -> None:
    """Списывает лимит по идентификатору браузера, сессии и весу пачки.

    Лимит по IP, который ведёт middleware, слаб с ОБЕИХ сторон. Против ботнета
    он бесполезен: тысяча адресов — тысяча независимых лимитов. По легитимным
    пользователям он, наоборот, бьёт: за корпоративным NAT или за мобильным
    оператором под одним адресом сидят тысячи человек, и активность одного
    отбирает лимит у остальных.

    ``anonymous_id`` и ``session_id`` подделываются тривиально (это значения из
    браузера), поэтому они не заменяют лимит по IP, а дополняют его: обойти
    надо оба сразу, а не одно из двух.

    ВСЕ списания уходят в Redis ОДНИМ pipeline. Это горячий путь приёма: раньше
    каждая идентичность проверялась отдельным ``await``, и на одно событие
    приходилось до трёх последовательных round-trip, задержки которых
    складывались. Число ключей на запрос при этом не изменилось — изменилось
    число обращений к Redis.

    Списание агрегировано по идентичностям: пачка из 50 меток прогресса одной
    сессии — это одно списание на 50 единиц, а не пятьдесят команд.

    Отказ Redis игнорируется по общему правилу сервиса (fail-open).
    """
    if not settings.UGC_RATE_LIMIT_ENABLED:
        return
    redis = get_redis()
    if redis is None:
        return

    charges: list[Charge] = []

    # Middleware уже списало одну единицу лимита за сам HTTP-запрос; здесь
    # добираем остальное, чтобы упаковка событий в пачку не позволяла обойти
    # ограничение. enforce=False: превышение не отклоняет уже принятую пачку —
    # оно лишь исчерпает лимит, и следующий запрос клиента получит 429.
    if len(events) > 1:
        charges.append(Charge(identity=request_context.ip or 'unknown', cost=len(events) - 1, enforce=False))

    if settings.UGC_RATE_LIMIT_BY_IDENTITY:
        costs: Counter[str] = Counter()
        for event in events:
            if event.anonymous_id:
                costs[f'aid:{event.anonymous_id}'] += 1
            costs[f'sid:{event.session_id}'] += 1
        charges.extend(
            Charge(identity=identity, cost=cost, limit=settings.UGC_IDENTITY_RATE_LIMIT_TIMES)
            for identity, cost in costs.items()
        )

    if not charges:
        return

    allowed, retry_after = await RateLimiter(redis).consume_many(charges)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too Many Requests',
            headers={'Retry-After': str(retry_after)},
        )
