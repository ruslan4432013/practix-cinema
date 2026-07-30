"""Агрегированный рейтинг фильма.

Отдельный префикс, а не `/api/v1/films/{id}/rating`: `/api/v1/films` за Nginx
принадлежит Movies API, и маршрут с таким префиксом увёл бы у него весь каталог.

Ручки «посчитать агрегат на лету» здесь нет намеренно. В исследовании она
мерилась отдельным сценарием и оказалась в 13 раз медленнее чтения преагрегата —
это единственное место, где 200 мс реально могли не выдержать. Раз выставлять
наружу медленный путь незачем, его и нет.
"""

import uuid

from fastapi import APIRouter, Depends

from practix_ugc_api.api.v1.dependencies import get_rating_service
from practix_ugc_api.models.schemas import FilmRatingResponse
from practix_ugc_api.services.rating_service import RatingService

router = APIRouter()


@router.get(
    '/{film_id}',
    response_model=FilmRatingResponse,
    summary='Рейтинг фильма',
    description=(
        'Читается из преагрегата одним обращением по первичному ключу. '
        'Лайки и дизлайки выводятся из гистограммы оценок по текущему порогу, '
        'поэтому смена порога не требует пересчёта данных.\n\n'
        'Фильм, которого никто не оценивал, возвращает нули и `average: null` — '
        'это нормальный ответ, а не отсутствие ресурса.'
    ),
)
async def film_rating(
    film_id: uuid.UUID,
    service: RatingService = Depends(get_rating_service),
) -> FilmRatingResponse:
    return await service.get_film_rating(film_id)
