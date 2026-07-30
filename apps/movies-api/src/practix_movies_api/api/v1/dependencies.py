import json

from async_fastapi_jwt_auth import AuthJWT
from fastapi import Depends, HTTPException, Request, status
from redis.asyncio import Redis

# Реэкспорт через `as`: роутеры импортируют PaginationParams отсюда, и менять их
# незачем — класс был побайтово одинаков с версией в Auth и переехал в
# practix_core. Форма `X as X` помечает реэкспорт намеренным для линтера, не
# затрагивая при этом семантику `import *` для остальных имён модуля.
from practix_core.pagination import PaginationParams as PaginationParams
from practix_movies_api.db.redis_db import get_redis
from practix_movies_api.services.auth_client import AuthServiceClient, get_auth_client

# Роли, дающие доступ к контенту по подписке.
SUBSCRIBER_ROLES = {'subscriber', 'admin', 'superuser'}


def _extract_bearer_token(request: Request) -> str:
    """Достаёт сырой JWT из заголовка Authorization (или '' если его нет)."""
    header = request.headers.get('Authorization', '')
    parts = header.split()
    if len(parts) == 2 and parts[0].lower() == 'bearer':
        return parts[1]
    return ''


async def get_user_roles(authorize: AuthJWT = Depends(), redis: Redis = Depends(get_redis)) -> list[str]:
    """Получает список ролей пользователя из JWT токена с кэшированием в Redis.

    Токен опционален: анонимный запрос вернёт пустой список.
    """
    await authorize.jwt_optional()
    user_id = await authorize.get_jwt_subject()

    if not user_id:
        return []

    # Пытаемся получить роли из кэша Redis
    cache_key = f'user:{user_id}:roles'
    cached_roles = await redis.get(cache_key)
    if cached_roles:
        return json.loads(cached_roles)

    # Если в кэше нет, берем из JWT
    raw_jwt = await authorize.get_raw_jwt()
    roles = raw_jwt.get('roles', []) if raw_jwt else []

    # Сохраняем в кэш на 5 минут
    await redis.set(cache_key, json.dumps(roles), ex=300)

    return roles


async def get_auth_context(
    request: Request,
    authorize: AuthJWT = Depends(),
) -> dict:
    """Опциональный контекст аутентификации для эндпоинтов со смешанным доступом.

    Возвращает ``{"sub", "roles", "token"}``. Для анонимного пользователя
    ``sub`` и ``token`` равны ``None``, ``roles`` — пустой список. Проверка
    токена локальная (общий секрет), поэтому не зависит от доступности Auth.
    """
    await authorize.jwt_optional()
    user_id = await authorize.get_jwt_subject()
    if not user_id:
        return {'sub': None, 'roles': [], 'token': None}

    raw_jwt = await authorize.get_raw_jwt()
    roles = raw_jwt.get('roles', []) if raw_jwt else []
    return {'sub': user_id, 'roles': roles, 'token': _extract_bearer_token(request)}


async def get_current_user_required(authorize: AuthJWT = Depends()) -> dict:
    """Требует валидный токен (иначе 401 через обработчик AuthJWTException)."""
    await authorize.jwt_required()
    user_id = await authorize.get_jwt_subject()
    raw_jwt = await authorize.get_raw_jwt()
    roles = raw_jwt.get('roles', []) if raw_jwt else []
    return {'sub': user_id, 'roles': roles}


async def ensure_subscription_access(context: dict, redis: Redis) -> None:
    """Проверяет доступ к контенту по подписке с изящной деградацией.

    - Анонимный пользователь -> 401.
    - Роль subscriber/admin уже в токене -> доступ (без запроса в Auth).
    - Иначе опрашиваем Auth-сервис за актуальным статусом подписки:
        * Auth ответил и разрешил -> доступ;
        * Auth ответил и запретил -> 403;
        * Auth недоступен -> опираемся на роли из токена (тут их нет) -> 403.
      Ни при каких обстоятельствах не возвращаем 5xx из-за падения Auth.
    """
    if not context.get('sub'):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Требуется авторизация',
        )

    token_roles = set(context.get('roles', []))
    if token_roles & SUBSCRIBER_ROLES:
        return

    auth_client = AuthServiceClient(get_auth_client(), redis)
    result = await auth_client.check_permissions(context['token'], ['subscriber'])

    if result is not None:
        if result.get('allowed') or result.get('is_superuser'):
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Требуется подписка',
        )

    # Auth недоступен: изящная деградация — доверяем только ролям из токена.
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail='Требуется подписка',
    )


async def get_current_user(roles: list[str] = Depends(get_user_roles)) -> dict:
    """Получение текущего пользователя (ролей)."""
    return {'roles': roles}
