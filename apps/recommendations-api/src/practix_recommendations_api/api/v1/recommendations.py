"""Три ручки выдачи. В запросе не считается ничего — только чтение готового.

Разрез «батч думает, выдача читает» задан ADR-001: коллаборативная фильтрация в
момент запроса — это перемножение матриц на сотнях тысяч строк, а бюджет ответа
— 300 мс. Поэтому здесь нет ни одной строчки арифметики модели: порядок позиций
посчитан заранее и лежит в витрине колонкой ``rank``.

Ни одна ручка не отвечает 5xx по причине отказа источника — см.
``services/degradation.py``. Единственный не-200 по существу — 404 на
неизвестный фильм (F2.4) и 401 на персональной выдаче без токена.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from practix_recommendations_api.api.v1.dependencies import (
    AUTH_RESPONSES,
    get_catalog_client,
    get_current_user_id,
    get_recommendation_service,
    limit_param,
)
from practix_recommendations_api.core.config import settings
from practix_recommendations_api.models.schemas import RecommendationItem, RecommendationsResponse
from practix_recommendations_api.services import metrics
from practix_recommendations_api.services.catalog_client import CatalogClient
from practix_recommendations_api.services.degradation import FilmNotFound, Recommendations, RecommendationService

router = APIRouter()

ENRICH_DESCRIPTION = (
    'Дорисовать названия из Movies API. По умолчанию выключено: каталог принадлежит Movies API, '
    'и полные карточки фронт берёт там же. Ограничено RECS_ENRICH_MAX позициями; при недоступном '
    'каталоге позиции остаются без названия, а не пропадают.'
)


async def _render(
    result: Recommendations,
    *,
    enrich: bool,
    catalog: CatalogClient | None,
) -> RecommendationsResponse:
    items = [RecommendationItem(film_id=film_id, score=score) for film_id, score in result.items]

    if enrich and settings.RECS_ENRICH_ENABLED and catalog is not None and items:
        cards = await catalog.fetch([item.film_id for item in items])
        for item in items:
            card = cards.get(item.film_id)
            if card is not None:
                item.title, item.imdb_rating = card

    return RecommendationsResponse(source=result.source, version=result.version, items=items)


@router.get(
    '/similar/{film_id}',
    response_model=RecommendationsResponse,
    summary='Похожие фильмы',
    description=(
        'Фильмы, которые смотрят те же люди, по убыванию близости. Исходный фильм в выдачу не попадает.\n\n'
        'Если у фильма ещё нет рассчитанных соседей — это **200 с популярным**, а не 404 и не 5xx: '
        'блок на странице фильма обязан отрисоваться. Поле `source` показывает, что именно отдано.'
    ),
    response_description='До N идентификаторов с весами и признаком источника',
    tags=['Рекомендации'],
    responses={status.HTTP_404_NOT_FOUND: {'description': 'Фильма нет в каталоге'}},
)
async def similar_films(
    film_id: uuid.UUID,
    limit: int = Depends(limit_param),
    enrich: bool = False,
    service: RecommendationService = Depends(get_recommendation_service),
    catalog: CatalogClient | None = Depends(get_catalog_client),
) -> RecommendationsResponse:
    with metrics.response_duration.labels(kind='similar').time():
        try:
            result = await service.similar(film_id, limit)
        except FilmNotFound as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Фильм не найден') from exc
        return await _render(result, enrich=enrich, catalog=catalog)


@router.get(
    '/popular',
    response_model=RecommendationsResponse,
    summary='Популярное',
    description=(
        'Топ фильмов за настраиваемое окно, посчитанный по завершённым просмотрам. '
        'Отдаётся **без токена** — это блок для анонимного посетителя и одновременно путь деградации '
        'для двух остальных ручек.\n\n'
        'Пустая витрина (обучение ещё не проходило) — это 200 с пустым списком, а не ошибка.'
    ),
    tags=['Рекомендации'],
)
async def popular_films(
    limit: int = Depends(limit_param),
    enrich: bool = False,
    service: RecommendationService = Depends(get_recommendation_service),
    catalog: CatalogClient | None = Depends(get_catalog_client),
) -> RecommendationsResponse:
    with metrics.response_duration.labels(kind='popular').time():
        return await _render(await service.popular(limit), enrich=enrich, catalog=catalog)


@router.get(
    '/me',
    response_model=RecommendationsResponse,
    summary='Рекомендуем вам',
    description=(
        'Персональная подборка. `user_id` берётся **только из токена** — параметром его передать нельзя, '
        'иначе выдача стала бы оракулом по чужой истории просмотров.\n\n'
        'Уже просмотренное исключено ещё на этапе обучения. Пользователь без истории получает популярное '
        'с `source=popular`, а не пустой ответ.'
    ),
    tags=['Рекомендации'],
    responses=AUTH_RESPONSES,
)
async def personal_films(
    limit: int = Depends(limit_param),
    enrich: bool = False,
    user_id: uuid.UUID = Depends(get_current_user_id),
    service: RecommendationService = Depends(get_recommendation_service),
    catalog: CatalogClient | None = Depends(get_catalog_client),
) -> RecommendationsResponse:
    with metrics.response_duration.labels(kind='personal').time():
        return await _render(await service.personal(user_id, limit), enrich=enrich, catalog=catalog)
