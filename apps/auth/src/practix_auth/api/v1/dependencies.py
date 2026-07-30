from async_fastapi_jwt_auth import AuthJWT
from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from practix_auth.core.config import settings
from practix_auth.db.postgres import get_session
from practix_auth.models.entity import User
from practix_auth.services.auth_service import AuthService
from practix_auth.services.oauth_service import OAuthService
from practix_auth.services.role_service import RoleService

# Реэкспорт через `as`: роутеры импортируют PaginationParams отсюда, и менять их
# незачем — класс был побайтово одинаков с версией в Movies API и переехал в
# practix_core. Форма `X as X` помечает реэкспорт намеренным для линтера, не
# затрагивая при этом семантику `import *` для остальных имён модуля.
from practix_core.pagination import PaginationParams as PaginationParams

# Роли, которым автоматически разрешён доступ ко всем защищённым ручкам.
SUPERUSER_ROLES = settings.SUPERUSER_ROLES


async def get_auth_service(db: AsyncSession = Depends(get_session), authorize: AuthJWT = Depends()) -> AuthService:
    """Получение сервиса аутентификации."""
    return AuthService(db, authorize)


async def get_role_service(db: AsyncSession = Depends(get_session)) -> RoleService:
    """Получение сервиса управления ролями."""
    return RoleService(db)


async def get_oauth_service(db: AsyncSession = Depends(get_session), authorize: AuthJWT = Depends()) -> OAuthService:
    """Получение сервиса входа через соцсети (OAuth)."""
    return OAuthService(db, authorize)


async def get_current_user(db: AsyncSession = Depends(get_session), authorize: AuthJWT = Depends()) -> User:
    """
    Получение текущего пользователя из БД.
    Используется там, где нужен полный объект пользователя.
    """
    await authorize.jwt_required()
    user_id = await authorize.get_jwt_subject()

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Пользователь не найден')
    return user


async def get_user_roles_from_jwt(authorize: AuthJWT = Depends()) -> list[str]:
    """
    Получение ролей пользователя напрямую из JWT токена.
    Оптимизировано: не требует запроса к базе данных.
    """
    await authorize.jwt_required()
    raw_jwt = await authorize.get_raw_jwt()
    return raw_jwt.get('roles', []) or []


def role_required(allowed_roles: list[str]):
    """
    Зависимость для проверки прав доступа.
    Использует роли из JWT для повышения производительности.
    Суперпользователь (роль 'admin' или 'superuser') имеет доступ ко всем ручкам.
    """

    async def role_checker(user_roles: list[str] = Depends(get_user_roles_from_jwt)):
        roles_set = set(user_roles)
        # Bypass для суперпользователя / администратора
        if roles_set & settings.SUPERUSER_ROLES:
            return user_roles
        if not roles_set.intersection(allowed_roles):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Недостаточно прав')
        return user_roles

    return role_checker
