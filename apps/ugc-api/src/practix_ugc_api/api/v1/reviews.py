"""Рецензии и голосование за них."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from practix_ugc_api.api.v1.dependencies import (
    AUTH_RESPONSES,
    PaginationParams,
    get_current_user_id,
    get_review_service,
    is_moderator,
    page_slice,
)
from practix_ugc_api.models.schemas import (
    ReviewCreateRequest,
    ReviewListItem,
    ReviewResponse,
    ReviewSort,
    ReviewVotesResponse,
    VoteRequest,
)
from practix_ugc_api.services.exceptions import ConflictError, ForbiddenError, NotFoundError
from practix_ugc_api.services.review_service import ReviewService

router = APIRouter()

_NOT_FOUND = {status.HTTP_404_NOT_FOUND: {'description': 'Рецензия не найдена'}}


@router.get(
    '',
    response_model=list[ReviewListItem],
    summary='Рецензии на фильм',
    description=(
        'Три порядка сортировки: `new` — по дате, `useful` — по полезности '
        '(голоса «за» минус голоса «против»), `rating` — по оценке автора рецензии. '
        'На каждый порядок в схеме заведён свой индекс.\n\n'
        'Тело рецензии в списке не отдаётся — за ним нужен запрос конкретной рецензии.'
    ),
)
async def list_reviews(
    film_id: uuid.UUID = Query(description='Фильм, рецензии на который нужны'),
    sort: ReviewSort = Query(default=ReviewSort.USEFUL, description='Порядок сортировки'),
    pagination: PaginationParams = Depends(PaginationParams),
    service: ReviewService = Depends(get_review_service),
) -> list[ReviewListItem]:
    limit, offset = page_slice(pagination)
    return await service.list_for_film(film_id, sort, limit, offset)


@router.post(
    '',
    response_model=ReviewResponse,
    status_code=status.HTTP_201_CREATED,
    summary='Написать рецензию',
    responses={
        **AUTH_RESPONSES,
        status.HTTP_409_CONFLICT: {'description': 'У пользователя уже есть рецензия на этот фильм'},
    },
)
async def create_review(
    request: ReviewCreateRequest,
    user_id: uuid.UUID = Depends(get_current_user_id),
    service: ReviewService = Depends(get_review_service),
) -> ReviewResponse:
    try:
        return await service.create(user_id, request)
    except ConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get(
    '/{review_id}',
    response_model=ReviewResponse,
    summary='Рецензия целиком',
    responses=_NOT_FOUND,
)
async def get_review(
    review_id: uuid.UUID,
    service: ReviewService = Depends(get_review_service),
) -> ReviewResponse:
    try:
        return await service.get(review_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.delete(
    '/{review_id}',
    status_code=status.HTTP_204_NO_CONTENT,
    summary='Удалить рецензию',
    description='Свою — всегда, чужую — только с ролью модератора.',
    responses={
        **AUTH_RESPONSES,
        **_NOT_FOUND,
        status.HTTP_403_FORBIDDEN: {'description': 'Рецензия принадлежит другому пользователю'},
    },
)
async def delete_review(
    review_id: uuid.UUID,
    user_id: uuid.UUID = Depends(get_current_user_id),
    moderator: bool = Depends(is_moderator),
    service: ReviewService = Depends(get_review_service),
) -> Response:
    try:
        await service.delete(review_id, user_id, moderator)
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ForbiddenError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put(
    '/{review_id}/vote',
    response_model=ReviewVotesResponse,
    summary='Оценить полезность рецензии',
    description='Идемпотентно: повторный такой же голос ничего не меняет, противоположный — переголосовывает.',
    responses={**AUTH_RESPONSES, **_NOT_FOUND},
)
async def vote(
    review_id: uuid.UUID,
    request: VoteRequest,
    user_id: uuid.UUID = Depends(get_current_user_id),
    service: ReviewService = Depends(get_review_service),
) -> ReviewVotesResponse:
    try:
        return await service.vote(review_id, user_id, request.value)
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.delete(
    '/{review_id}/vote',
    status_code=status.HTTP_204_NO_CONTENT,
    summary='Отозвать свой голос',
    responses={**AUTH_RESPONSES, **_NOT_FOUND},
)
async def retract_vote(
    review_id: uuid.UUID,
    user_id: uuid.UUID = Depends(get_current_user_id),
    service: ReviewService = Depends(get_review_service),
) -> Response:
    try:
        retracted = await service.retract_vote(review_id, user_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if retracted is None:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
