"""Зависимости FastAPI: личность отправителя и контекст запроса.

ГЛАВНОЕ РЕШЕНИЕ ЭТОГО МОДУЛЯ — аутентификация опциональна и никогда не
приводит к отказу. Аргументы:

* значительная часть ценной аналитики (главная страница, поиск, промо)
  происходит до входа пользователя — требовать токен значило бы её не собирать;
* просроченный или отозванный токен не повод потерять событие: пользователь
  всё равно совершил действие, просто атрибутировать его придётся как
  анонимное;
* Auth — «горячая» зависимость, и по требованию спринта её недоступность не
  должна ломать сайт. Проверка JWT здесь выполняется **локально**, по общему
  секрету, без сетевых обращений к Auth-сервису, поэтому его падение вообще не
  влияет на приём событий.

При этом безопасность не ослаблена: ``user_id`` берётся исключительно из
подписи токена. Невалидная подпись означает анонимное событие, а не событие от
чужого имени.
"""

import logging
from uuid import UUID

from async_fastapi_jwt_auth import AuthJWT
from fastapi import Depends, Request

from core.privacy import get_client_ip
from core.request_id import get_request_id
from services.event_service import Principal, RequestContext

logger = logging.getLogger(__name__)


async def get_principal(authorize: AuthJWT = Depends()) -> Principal:
    """Определяет пользователя по Bearer-токену, если он есть и валиден."""
    try:
        # jwt_optional не требует токена, но проверяет подпись, срок действия
        # и denylist, если токен всё же прислан.
        await authorize.jwt_optional()
        subject = await authorize.get_jwt_subject()
    except Exception as exc:  # noqa: BLE001 — любая проблема с токеном => аноним
        logger.debug('Anonymous event: token verification failed (%s)', exc)
        return Principal()

    if not subject:
        return Principal()

    try:
        return Principal(user_id=UUID(str(subject)), is_authenticated=True)
    except (ValueError, TypeError):
        # sub есть, но это не UUID — токен выпущен не нашим Auth-сервисом.
        logger.warning('Token subject is not a valid UUID, treating event as anonymous')
        return Principal()


async def get_request_context(request: Request) -> RequestContext:
    """Собирает серверные данные запроса (их клиент подделать не может)."""
    headers = {name.lower(): value for name, value in request.headers.items()}
    return RequestContext(
        ip=get_client_ip(headers, request.client.host if request.client else None),
        user_agent=headers.get('user-agent'),
        request_id=get_request_id(),
    )
