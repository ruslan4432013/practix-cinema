"""Схемы запросов и ответов.

У запросов ``extra='forbid'`` — правило коллектора, действующее и здесь по той
же причине: ``user_id`` берётся ИСКЛЮЧИТЕЛЬНО из подписи токена, и поля с таким
именем в теле быть не должно. Молчаливое игнорирование лишнего поля означало бы,
что клиент, приславший ``{"rating": 9, "user_id": "..."}``, считает, что оценил
за другого, — и не узнает, что это не сработало.
"""

import uuid
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from practix_ugc_api.core.config import settings
from practix_ugc_api.services.aggregate import HISTOGRAM_SIZE


class ReviewSort(str, Enum):
    """Поддерживаемые порядки сортировки рецензий.

    Перечисление, а не свободная строка: имя колонки в ``ORDER BY`` параметром
    не передаётся, и подстановка идёт только из белого списка — иначе это
    SQL-инъекция.
    """

    NEW = 'new'
    USEFUL = 'useful'
    RATING = 'rating'


class RatingRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    rating: int = Field(ge=0, le=10, description='Оценка фильма: целое 0..10. Дизлайк — 0, лайк — 10')


class FilmRatingResponse(BaseModel):
    film_id: uuid.UUID
    ratings_count: int = Field(description='Сколько всего оценок у фильма')
    average: float | None = Field(description='Средняя оценка; null, если оценок ещё нет')
    likes: int = Field(description='Число оценок не ниже порога like_threshold')
    dislikes: int = Field(description='Число оценок ниже порога like_threshold')
    # Порог возвращается в ответе, чтобы клиент не мог разойтись с сервером в
    # том, что считается лайком: порог продуктовый и меняется настройкой.
    like_threshold: int = Field(description='Порог, с которого оценка считается лайком')
    histogram: list[int] = Field(
        min_length=HISTOGRAM_SIZE,
        max_length=HISTOGRAM_SIZE,
        description='Распределение оценок: histogram[i] — сколько раз выставлена оценка i',
    )


class LikeResponse(BaseModel):
    film_id: uuid.UUID
    rating: int
    film_rating: FilmRatingResponse = Field(description='Агрегат фильма уже с учётом этой оценки')


class LikedFilm(BaseModel):
    film_id: uuid.UUID
    rating: int
    updated_at: datetime


class BookmarkResponse(BaseModel):
    film_id: uuid.UUID
    created_at: datetime


class ReviewCreateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    film_id: uuid.UUID
    body: str = Field(min_length=1, max_length=settings.UGC_API_REVIEW_MAX_LENGTH)
    author_rating: int | None = Field(
        default=None, ge=0, le=10, description='Оценка автора рецензии, если он её ставил'
    )


class ReviewResponse(BaseModel):
    review_id: uuid.UUID
    film_id: uuid.UUID
    user_id: uuid.UUID
    body: str
    author_rating: int | None
    created_at: datetime
    votes_likes: int
    votes_dislikes: int
    useful_score: int = Field(description='votes_likes - votes_dislikes; по нему идёт сортировка «по полезности»')


class ReviewListItem(BaseModel):
    """Строка списка рецензий — без тела.

    Тело не отдаётся в списке намеренно: двадцать рецензий по четыре килобайта
    превращают выдачу каталога в 80 КБ, из которых пользователь прочтёт одну.
    """

    review_id: uuid.UUID
    film_id: uuid.UUID
    user_id: uuid.UUID
    author_rating: int | None
    created_at: datetime
    votes_likes: int
    votes_dislikes: int
    useful_score: int


class VoteRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    value: Literal[-1, 1] = Field(description='+1 — рецензия полезна, -1 — бесполезна')


class ReviewVotesResponse(BaseModel):
    review_id: uuid.UUID
    votes_likes: int
    votes_dislikes: int
    useful_score: int
