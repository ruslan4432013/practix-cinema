"""Зависимости FastAPI: личность пользователя и сервисный слой.

``user_id`` берётся ИСКЛЮЧИТЕЛЬНО из подписи токена. Своей таблицы пользователей
у сервиса нет и быть не должно: второй источник правды о том, кто есть кто,
рано или поздно разойдётся с Auth. Проверка подписи локальная, по общему
секрету, — падение Auth не мешает ставить оценки.

Отличие от коллектора: там ``jwt_optional`` и аноним вместо отказа, потому что
потерянное событие аналитики дешевле отказа. Здесь запись именная по
определению — без токена сохранять нечего, поэтому ``jwt_required`` и 401.
"""

import uuid

from async_fastapi_jwt_auth import AuthJWT
from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

# Форма `X as X` помечает намеренный реэкспорт: роутеры импортируют пагинацию
# отсюда, как и в movies-api, а не тянут её из библиотеки каждый по-своему.
from practix_core.pagination import PaginationParams as PaginationParams
from practix_ugc_api.core.config import settings
from practix_ugc_api.db.postgres import get_session
from practix_ugc_api.services.bookmark_service import BookmarkService
from practix_ugc_api.services.rating_service import RatingService
from practix_ugc_api.services.review_service import ReviewService

# Ответы, общие почти для всех ручек. Вынесены, чтобы не повторять их в каждом
# декораторе и не разъехаться в формулировках.
AUTH_RESPONSES: dict[int | str, dict] = {
    status.HTTP_401_UNAUTHORIZED: {'description': 'Токен отсутствует, недействителен или отозван'},
}


async def get_current_user_id(authorize: AuthJWT = Depends()) -> uuid.UUID:
    """Идентификатор пользователя из подписанного токена."""
    await authorize.jwt_required()
    subject = await authorize.get_jwt_subject()
    try:
        return uuid.UUID(str(subject))
    except (ValueError, TypeError) as exc:
        # sub есть, но это не UUID — токен выпущен не нашим Auth-сервисом.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Некорректный субъект токена') from exc


async def get_user_roles(authorize: AuthJWT = Depends()) -> list[str]:
    """Роли из claim'а ``roles``.

    Читаются из токена, а не из базы Auth: у этого сервиса доступа к ней нет, да
    и сетевой вызов на каждый запрос ради проверки прав — плохой размен.
    """
    await authorize.jwt_required()
    raw_jwt = await authorize.get_raw_jwt()
    return raw_jwt.get('roles', []) or []


async def is_moderator(roles: list[str] = Depends(get_user_roles)) -> bool:
    """Может ли пользователь трогать чужой контент."""
    return bool(set(roles) & settings.SUPERUSER_ROLES)


def page_slice(pagination: PaginationParams) -> tuple[int, int]:
    """``PaginationParams`` в пару ``(limit, offset)`` для SQL."""
    return pagination.page_size, (pagination.page_number - 1) * pagination.page_size


async def get_rating_service(db: AsyncSession = Depends(get_session)) -> RatingService:
    return RatingService(db)


async def get_bookmark_service(db: AsyncSession = Depends(get_session)) -> BookmarkService:
    return BookmarkService(db)


async def get_review_service(db: AsyncSession = Depends(get_session)) -> ReviewService:
    return ReviewService(db)
