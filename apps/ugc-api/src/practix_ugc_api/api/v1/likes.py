"""Оценки фильмов.

«Лайк» и «дизлайк» отдельных ручек не имеют: это частные случаи оценки 10 и 0.
Одна операция вместо трёх — потому что и в хранилище это одна строка, и потому
что порог, с которого оценка считается лайком, продуктовый и меняется (см.
``UGC_API_LIKE_THRESHOLD``).
"""

import uuid

from fastapi import APIRouter, Depends, Query, Response, status

from practix_ugc_api.api.v1.dependencies import (
    AUTH_RESPONSES,
    PaginationParams,
    get_current_user_id,
    get_rating_service,
    page_slice,
)
from practix_ugc_api.core.config import settings
from practix_ugc_api.models.schemas import LikedFilm, LikeResponse, RatingRequest
from practix_ugc_api.services.rating_service import RatingService

router = APIRouter()


@router.get(
    '/me',
    response_model=list[LikedFilm],
    summary='Понравившиеся фильмы',
    description=(
        'Фильмы, которым текущий пользователь поставил оценку не ниже `min_rating`, '
        'от высоких оценок к низким и от свежих к старым.'
    ),
    responses=AUTH_RESPONSES,
)
async def liked_films(
    min_rating: int = Query(
        default=settings.UGC_API_LIKED_MIN_RATING,
        ge=0,
        le=10,
        description='Нижняя граница оценки, начиная с которой фильм считается понравившимся',
    ),
    pagination: PaginationParams = Depends(PaginationParams),
    user_id: uuid.UUID = Depends(get_current_user_id),
    service: RatingService = Depends(get_rating_service),
) -> list[LikedFilm]:
    limit, offset = page_slice(pagination)
    return await service.list_liked_films(user_id, min_rating, limit, offset)


@router.put(
    '/{film_id}',
    response_model=LikeResponse,
    summary='Поставить или изменить оценку',
    description=(
        'Идемпотентно: повторная отправка той же оценки ничего не меняет. '
        'В ответе — агрегат фильма уже с учётом этой оценки, чтобы клиенту не '
        'приходилось делать второй запрос ради обновления счётчиков.'
    ),
    responses=AUTH_RESPONSES,
)
async def set_rating(
    film_id: uuid.UUID,
    request: RatingRequest,
    user_id: uuid.UUID = Depends(get_current_user_id),
    service: RatingService = Depends(get_rating_service),
) -> LikeResponse:
    film_rating = await service.set_rating(user_id, film_id, request.rating)
    return LikeResponse(film_id=film_id, rating=request.rating, film_rating=film_rating)


@router.delete(
    '/{film_id}',
    status_code=status.HTTP_204_NO_CONTENT,
    summary='Снять оценку',
    responses={
        **AUTH_RESPONSES,
        status.HTTP_404_NOT_FOUND: {'description': 'Пользователь этот фильм не оценивал'},
    },
)
async def delete_rating(
    film_id: uuid.UUID,
    user_id: uuid.UUID = Depends(get_current_user_id),
    service: RatingService = Depends(get_rating_service),
) -> Response:
    removed = await service.delete_rating(user_id, film_id)
    if removed is None:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
