"""Закладки «посмотреть позже»."""

import uuid

from fastapi import APIRouter, Depends, Response, status

from practix_ugc_api.api.v1.dependencies import (
    AUTH_RESPONSES,
    PaginationParams,
    get_bookmark_service,
    get_current_user_id,
    page_slice,
)
from practix_ugc_api.models.schemas import BookmarkResponse
from practix_ugc_api.services.bookmark_service import BookmarkService

router = APIRouter()


@router.get(
    '',
    response_model=list[BookmarkResponse],
    summary='Закладки пользователя',
    description='Свежие сверху.',
    responses=AUTH_RESPONSES,
)
async def list_bookmarks(
    pagination: PaginationParams = Depends(PaginationParams),
    user_id: uuid.UUID = Depends(get_current_user_id),
    service: BookmarkService = Depends(get_bookmark_service),
) -> list[BookmarkResponse]:
    limit, offset = page_slice(pagination)
    return await service.list_for_user(user_id, limit, offset)


@router.put(
    '/{film_id}',
    response_model=BookmarkResponse,
    summary='Добавить закладку',
    description='Идемпотентно: повторный вызов не сдвигает дату добавления.',
    responses=AUTH_RESPONSES,
)
async def add_bookmark(
    film_id: uuid.UUID,
    user_id: uuid.UUID = Depends(get_current_user_id),
    service: BookmarkService = Depends(get_bookmark_service),
) -> BookmarkResponse:
    return await service.add(user_id, film_id)


@router.delete(
    '/{film_id}',
    status_code=status.HTTP_204_NO_CONTENT,
    summary='Удалить закладку',
    responses={**AUTH_RESPONSES, status.HTTP_404_NOT_FOUND: {'description': 'Закладки не было'}},
)
async def delete_bookmark(
    film_id: uuid.UUID,
    user_id: uuid.UUID = Depends(get_current_user_id),
    service: BookmarkService = Depends(get_bookmark_service),
) -> Response:
    if not await service.remove(user_id, film_id):
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
