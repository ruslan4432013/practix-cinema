"""Зависимости FastAPI: личность пользователя и сервисный слой.

``user_id`` берётся ИСКЛЮЧИТЕЛЬНО из подписи токена. Своей таблицы пользователей
у сервиса нет и быть не должно: второй источник правды о том, кто есть кто,
рано или поздно разойдётся с Auth. Проверка подписи локальная, по общему
секрету, — падение Auth не мешает ставить оценки.

Отличие от коллектора: там ``jwt_optional`` и аноним вместо отказа, потому что
потерянное событие аналитики дешевле отказа. Здесь запись именная по
определению — без токена сохранять нечего, поэтому ``jwt_required`` и 401.

ОДИН ``jwt_required()`` НА ЗАПРОС. Он не просто разбирает токен: внутри него
проверяется денилист, то есть каждый вызов — поход в Redis Auth-сервиса. Пока
субъект и роли доставались двумя независимыми зависимостями, ``DELETE`` рецензии
(единственная ручка, которой нужны обе) платил за это двумя round-trip. Поэтому
токен разбирается один раз в ``get_principal``, а ``get_current_user_id`` и
``is_moderator`` собраны поверх него — FastAPI кэширует результат зависимости в
пределах запроса, так что двух вызовов не возникает.
"""

import uuid
from dataclasses import dataclass

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


@dataclass(frozen=True, slots=True)
class Principal:
    """Кто пришёл: субъект и роли, снятые с токена за один его разбор."""

    user_id: uuid.UUID
    roles: tuple[str, ...]


async def get_principal(authorize: AuthJWT = Depends()) -> Principal:
    """Единственное место, где вызывается ``jwt_required()`` — см. докстринг модуля."""
    await authorize.jwt_required()
    raw_jwt = await authorize.get_raw_jwt() or {}
    try:
        user_id = uuid.UUID(str(raw_jwt.get('sub')))
    except (ValueError, TypeError) as exc:
        # sub есть, но это не UUID — токен выпущен не нашим Auth-сервисом.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Некорректный субъект токена') from exc
    # Роли берутся из токена, а не из базы Auth: доступа к ней у сервиса нет, да
    # и сетевой вызов на каждый запрос ради проверки прав — плохой размен.
    return Principal(user_id=user_id, roles=tuple(raw_jwt.get('roles') or ()))


async def get_current_user_id(principal: Principal = Depends(get_principal)) -> uuid.UUID:
    """Идентификатор пользователя из подписанного токена."""
    return principal.user_id


async def is_moderator(principal: Principal = Depends(get_principal)) -> bool:
    """Может ли пользователь трогать чужой контент."""
    return bool(set(principal.roles) & settings.SUPERUSER_ROLES)


def page_slice(pagination: PaginationParams) -> tuple[int, int]:
    """``PaginationParams`` в пару ``(limit, offset)`` для SQL."""
    return pagination.page_size, (pagination.page_number - 1) * pagination.page_size


async def get_rating_service(db: AsyncSession = Depends(get_session)) -> RatingService:
    return RatingService(db)


async def get_bookmark_service(db: AsyncSession = Depends(get_session)) -> BookmarkService:
    return BookmarkService(db)


async def get_review_service(db: AsyncSession = Depends(get_session)) -> ReviewService:
    return ReviewService(db)
