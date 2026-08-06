"""Проверка токена пользователя в синхронном Django-сервисе.

## Почему это не переиспользование ``practix_core.jwt``

``practix_core.jwt`` — не декодер, а ОБВЯЗКА библиотеки ``async_fastapi_jwt_auth``:
``install_config_loader``, ``install_denylist_loader``, ``install_exception_handler``,
``make_jwt_settings``. Все четыре принимают или возвращают объекты FastAPI, и
вызвать из обработчика WSGI там нечего.

Своего кода разбора токена у FastAPI-сервисов при этом НЕТ: и коллектор, и UGC
API зовут ``await authorize.jwt_optional()``. То есть код ниже ничего не копирует —
оригинала не существует, и порог дублирования он не двигает.

Извлекать это в ``practix_core.jwt_claims`` (экстра ``jwt-decode = ["pyjwt"]``,
БЕЗ fastapi) следует в тот момент, когда появится ВТОРОЙ синхронный потребитель
локальной проверки токена. Раньше — нельзя: библиотека на одного потребителя это
не библиотека, и ``docs/monorepo.md`` уже дважды отказал по этой причине.
"""

import logging
from dataclasses import dataclass

import jwt
from django.http import HttpRequest

from practix_notifications.core.config import settings
from practix_notifications.core.redis import get_auth_redis

logger = logging.getLogger('notifications.jwt')

_BEARER_PREFIX = 'Bearer '


class TokenError(Exception):
    """Токена нет, он испорчен, просрочен или отозван. Наружу — 401."""


@dataclass(frozen=True)
class TokenClaims:
    subject: str
    jti: str
    roles: tuple[str, ...]


def bearer_token(request: HttpRequest) -> str | None:
    header = request.headers.get('Authorization', '')
    if not header.startswith(_BEARER_PREFIX):
        return None
    return header[len(_BEARER_PREFIX) :].strip() or None


def decode_access_token(token: str) -> TokenClaims:
    """Разобрать и проверить подпись access-токена. Сети не касается."""
    try:
        payload = jwt.decode(
            token,
            settings.AUTHJWT_SECRET_KEY,
            algorithms=[settings.NOTIFY_JWT_ALGORITHM],
            leeway=settings.NOTIFY_JWT_LEEWAY_SECONDS,
        )
    except jwt.PyJWTError as exc:
        raise TokenError(f'token is not valid: {exc}') from exc

    # Тип обязателен: refresh-токен живёт неделями, и открывать им кабинет
    # значило бы обесценить короткий срок жизни access-токена.
    if payload.get('type') != 'access':
        raise TokenError('access token is required')

    subject = payload.get('sub')
    jti = payload.get('jti')
    if not subject or not jti:
        raise TokenError('token has no subject or jti')

    roles = payload.get('roles') or []
    return TokenClaims(subject=str(subject), jti=str(jti), roles=tuple(str(role) for role in roles))


def is_revoked(jti: str) -> bool:
    """Отозван ли токен по данным Auth.

    Ключ денилиста — сам ``jti``: именно так его пишет
    ``practix_auth.services.auth_service`` при выходе и при ротации refresh.
    """
    if not settings.NOTIFY_DENYLIST_ENABLED:
        return False
    try:
        return get_auth_redis().get(jti) is not None
    except Exception as exc:
        policy = settings.NOTIFY_DENYLIST_ON_ERROR
        if policy == 'raise':
            raise
        logger.warning('Denylist check failed, Redis unavailable: %s', exc)
        # 'deny' — закрываемся: лента тем, о которых человеку писали, не должна
        # открываться по токену, про который мы не знаем, жив ли он.
        return policy == 'deny'


def authenticate(request: HttpRequest) -> TokenClaims:
    """Полная проверка: заголовок → подпись → отзыв. Всё остальное — ``TokenError``."""
    token = bearer_token(request)
    if token is None:
        raise TokenError('Authorization: Bearer <token> is required')
    claims = decode_access_token(token)
    if is_revoked(claims.jti):
        raise TokenError('token has been revoked')
    return claims
