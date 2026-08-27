"""Одноразовые ticket'ы для websocket-handshake.

## Зачем вообще ticket, если есть JWT

Браузер не умеет ставить заголовки в ``new WebSocket(url)``. Остаются три пути,
и у двух из них цена выше выгоды:

* ``?token=<JWT>`` — токен оседает в access-логе nginx и uvicorn, в истории
  браузера и в ``Referer``. Он живёт часами, и утёкшая строка лога — это готовый
  доступ к чужой ленте;
* ``Sec-WebSocket-Protocol: bearer, <JWT>`` — браузер умеет, но токен всё равно
  едет заголовком handshake и целиком попадает в тот же access-лог;
* **ticket** — случайная строка, живущая тридцать секунд и гасимая при первом
  использовании. В логе она остаётся, но к моменту, когда лог кто-то прочитает,
  она уже ничего не открывает.

## Гашение обязано быть атомарным

``GET`` + ``DEL`` двумя командами — это гонка: два одновременных handshake
успевают прочитать один ключ до того, как кто-то его удалит, и оба проходят.
``GETDEL`` (Redis 6.2+) делает это одной операцией, поэтому «одноразовый»
означает одноразовый, а не «обычно одноразовый».

## Что лежит внутри ticket'а

``sub``, ``jti`` и ``exp`` — ровно столько, сколько нужно, чтобы держать
соединение под контролем и не декодировать JWT второй раз. ``jti`` нужен для
периодической перепроверки денилиста (сокет живёт часами и переживает logout),
``exp`` — чтобы соединение не пережило собственный токен. Самого токена в
хранилище нет: ticket не должен быть способом ЕГО достать.
"""

import json
import secrets
from dataclasses import dataclass

from redis.asyncio import Redis

from practix_notifications_ws.core.config import settings


class TicketError(Exception):
    """Ticket отсутствует, уже погашен, протух или испорчен."""


@dataclass(frozen=True, slots=True)
class TicketPayload:
    """Личность, зафиксированная в момент выдачи ticket'а."""

    subject: str
    jti: str
    #: Unix-время истечения исходного access-токена; 0 — в токене не было ``exp``.
    expires_at: int


def _key(ticket: str) -> str:
    return f'{settings.NOTIFY_WS_TICKET_PREFIX}{ticket}'


async def issue(redis: Redis, payload: TicketPayload) -> str:
    """Выдать ticket. Секрет генерируется ``secrets``, а не ``random``."""
    ticket = secrets.token_urlsafe(32)
    body = json.dumps({'sub': payload.subject, 'jti': payload.jti, 'exp': payload.expires_at})
    await redis.set(_key(ticket), body, ex=settings.NOTIFY_WS_TICKET_TTL)
    return ticket


async def redeem(redis: Redis, ticket: str) -> TicketPayload:
    """Погасить ticket и вернуть личность. Второй раз тот же ticket не сработает."""
    if not ticket:
        raise TicketError('ticket is required')
    try:
        raw = await redis.getdel(_key(ticket))
    except Exception as exc:
        # Политика та же, что у денилиста: неизвестно — значит нет. Пускать по
        # непроверяемому ticket'у в поток чужих уведомлений нельзя.
        raise TicketError('ticket storage is unavailable') from exc
    if raw is None:
        raise TicketError('ticket is unknown, already used or expired')
    try:
        data = json.loads(raw)
        return TicketPayload(subject=str(data['sub']), jti=str(data['jti']), expires_at=int(data.get('exp', 0)))
    except (ValueError, TypeError, KeyError) as exc:
        raise TicketError('ticket payload is malformed') from exc
