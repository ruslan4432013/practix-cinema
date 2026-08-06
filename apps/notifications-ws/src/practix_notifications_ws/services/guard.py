"""Что стоит между чужим и потоком уведомлений.

Три рубежа, и каждый закрывает то, чего не закрывают остальные.

## 1. Origin

У WebSocket **нет** same-origin policy. Браузер откроет сокет к нашему шлюзу со
страницы любого сайта и, если сессия годится, отдаст туда данные — это
Cross-Site WebSocket Hijacking. Обычные ручки от него защищает CORS, сокет —
только явная проверка ``Origin`` на handshake.

Запрос без ``Origin`` браузером не отправлен: так приходят websocat, curl и
функциональные тесты. Защищать их от CSWSH нечего, поэтому по умолчанию они
пропускаются (``NOTIFY_WS_ALLOW_MISSING_ORIGIN``).

## 2. Одноразовый ticket

См. :mod:`practix_notifications_ws.services.tickets`.

## 3. Перепроверка на живом соединении

Сокет живёт часами, а токен отзывают в секунду: выйдя из системы, человек
разумно ожидает, что открытая вкладка перестанет получать его уведомления.
Разовой проверки на handshake для этого мало. Раз в
``NOTIFY_WS_REVALIDATE_SECONDS`` шлюз сверяет ``exp`` и ищет ``jti`` в денилисте
Auth — декодировать JWT для этого не нужно, оба поля уже лежат в ticket'е.

Политика при недоступности Redis — ``deny``, как в личном кабинете и UGC API
(коллектор — ``allow``, Auth — ``raise``). Держать открытым поток уведомлений по
токену, про который неизвестно, жив ли он, хуже, чем закрыть соединение: клиент
переподключится, а если Redis лежит — уедет на ленту, которая ничего от него не
требует.
"""

import logging
import time

from redis.asyncio import Redis

from practix_notifications_ws.core.config import settings
from practix_notifications_ws.services.tickets import TicketPayload

logger = logging.getLogger('notifications_ws.guard')

#: Код закрытия из приватного диапазона приложения (4000-4999). Стандартные
#: 1008/1011 клиент не отличит от «сервер перезапустился», а разница
#: принципиальная: на 4401 надо идти за новым токеном, а не переподключаться.
WS_CLOSE_UNAUTHORIZED = 4401
WS_CLOSE_FORBIDDEN = 4403
WS_CLOSE_TOO_MANY = 4429
WS_CLOSE_SHUTDOWN = 4503


def origin_allowed(origin: str | None) -> bool:
    """Разрешено ли открывать сокет с этой страницы."""
    if not origin:
        return settings.NOTIFY_WS_ALLOW_MISSING_ORIGIN
    return origin in settings.allowed_origins


def token_expired(payload: TicketPayload, *, now: float | None = None) -> bool:
    """Истёк ли исходный access-токен.

    ``exp == 0`` означает «в токене срока не было»: такие не выпускает наш Auth,
    но и придумывать за него срок жизни шлюз не должен.
    """
    if payload.expires_at <= 0:
        return False
    return (now if now is not None else time.time()) >= payload.expires_at


async def token_revoked(redis: Redis | None, jti: str) -> bool:
    """Лежит ли ``jti`` в денилисте Auth.

    Дублированием ``practix_core.jwt.install_denylist_loader`` это не является:
    тот загрузчик — часть обвязки ``async_fastapi_jwt_auth``, он вызывается
    внутри разбора токена и принимает разобранный токен. Здесь токена нет вовсе —
    есть ``jti`` из ticket'а и фоновая задача без HTTP-запроса вокруг.
    """
    if not jti:
        return False
    try:
        if redis is None:
            raise RuntimeError('redis client is not initialised')
        return await redis.get(jti) is not None
    # Ловится всё: что именно сломалось в Redis, здесь не важно — важно, что
    # ответ неизвестен, а как это трактовать, решает политика.
    except Exception as exc:
        if settings.NOTIFY_WS_DENYLIST_ON_ERROR == 'raise':
            raise
        logger.warning('Denylist re-check failed, Redis unavailable: %s', exc)
        return settings.NOTIFY_WS_DENYLIST_ON_ERROR == 'deny'


async def still_valid(redis: Redis | None, payload: TicketPayload, *, now: float | None = None) -> bool:
    """Можно ли ещё держать это соединение."""
    if token_expired(payload, now=now):
        return False
    return not await token_revoked(redis, payload.jti)
