from async_fastapi_jwt_auth import AuthJWT
from fastapi import Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from db.postgres import get_session
from models.entity import User
from services.auth_service import AuthService
from services.oauth_service import OAuthService
from services.role_service import RoleService

# Роли, которым автоматически разрешён доступ ко всем защищённым ручкам.
SUPERUSER_ROLES = settings.SUPERUSER_ROLES


class PaginationParams:
    def __init__(
        self,
        page_number: int = Query(1, ge=1, description='Номер страницы'),
        page_size: int = Query(50, ge=1, le=100, description='Размер страницы'),
    ):
        self.page_number = page_number
        self.page_size = page_size


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
