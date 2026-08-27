"""Выдача одноразового ticket'а — единственная дверь к websocket-ручке.

Это ОБЫЧНЫЙ HTTP-запрос, поэтому здесь работает штатная обвязка
``practix_core.jwt``: ``jwt_required()`` разбирает токен, проверяет подпись общим
с Auth секретом и заглядывает в денилист. Второго декодера JWT в репозитории не
появляется — тот, что описан в ``docs/notifications.md`` как «извлекать в
``practix_core.jwt_claims``, когда появится второй синхронный потребитель»,
по-прежнему ждёт своего случая: шлюз асинхронный и FastAPI-шный, обвязка ему
подходит как есть.

Вместе с ticket'ом отдаётся ПОЛИТИКА ДЕГРАДАЦИИ: адрес сокета, адрес long
polling'а, число попыток и паузы. Фронтенд не должен зашивать ни порт шлюза, ни
тайминги — они настраиваются в ``.env`` и обязаны меняться без пересборки
клиента.
"""

import logging

from async_fastapi_jwt_auth import AuthJWT
from fastapi import APIRouter, Depends, HTTPException, status

from practix_notifications_ws.core import redis as redis_db
from practix_notifications_ws.core.config import settings
from practix_notifications_ws.models.push import TicketResponse
from practix_notifications_ws.services import tickets
from practix_notifications_ws.services.tickets import TicketPayload

logger = logging.getLogger('notifications_ws.tickets')

router = APIRouter()


@router.post(
    '/ticket',
    response_model=TicketResponse,
    summary='Получить одноразовый ticket для websocket-подключения',
    description=(
        'Браузер не умеет ставить заголовки в `new WebSocket(url)`, поэтому токен на handshake '
        'не передаётся вовсе. Вместо него — ticket: случайная строка со сроком жизни '
        '`NOTIFY_WS_TICKET_TTL` секунд, которая гаснет при первом использовании.\n\n'
        'Ответ содержит и политику деградации: сколько раз пробовать websocket, с какой паузой '
        'и куда уходить на long polling, если он не поднялся.'
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {'description': 'Токен отсутствует, недействителен или отозван'},
        status.HTTP_503_SERVICE_UNAVAILABLE: {'description': 'Хранилище ticket’ов недоступно'},
    },
)
async def issue_ticket(authorize: AuthJWT = Depends()) -> TicketResponse:
    await authorize.jwt_required()
    raw_jwt = await authorize.get_raw_jwt() or {}
    subject = str(raw_jwt.get('sub') or '')
    jti = str(raw_jwt.get('jti') or '')
    if not subject or not jti:
        # Токен подписан нашим секретом, но выпущен не Auth-сервисом: без jti
        # соединение нечем перепроверять на отзыв.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Токен без субъекта или идентификатора')

    redis = await redis_db.get_client()
    if redis is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='Хранилище ticket’ов недоступно')
    try:
        ticket = await tickets.issue(
            redis, TicketPayload(subject=subject, jti=jti, expires_at=int(raw_jwt.get('exp') or 0))
        )
    # Любая ошибка Redis — это 503, а не 500: клиент должен понять, что дело в
    # шлюзе и что попытку имеет смысл повторить.
    except Exception as exc:
        logger.warning('Ticket storage is unavailable: %s', exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='Хранилище ticket’ов недоступно'
        ) from exc

    base = settings.NOTIFY_WS_PUBLIC_URL.rstrip('/')
    return TicketResponse(
        ticket=ticket,
        expires_in=settings.NOTIFY_WS_TICKET_TTL,
        ws_url=f'{base}/api/v1/ws?ticket={ticket}',
        # Схема у long polling'а http(s), а не ws(s): это обычный запрос.
        poll_url=f'{base.replace("wss://", "https://").replace("ws://", "http://")}/api/v1/ws/poll',
        reconnect_attempts=settings.NOTIFY_WS_RECONNECT_ATTEMPTS,
        reconnect_base_delay=settings.NOTIFY_WS_RECONNECT_BASE_DELAY,
        poll_timeout=settings.NOTIFY_WS_POLL_TIMEOUT,
        probe_interval=settings.NOTIFY_WS_PROBE_INTERVAL,
    )
