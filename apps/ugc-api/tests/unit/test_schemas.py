"""Валидация запросов — без базы и без сети.

Главная проверка здесь — ``extra='forbid'``: тело с полем ``user_id`` обязано
отвергаться, а не молча игнорироваться. Иначе клиент, приславший чужой
идентификатор, считал бы, что оценил за другого, и не узнал бы обратного.
"""

import uuid

import pytest
from pydantic import ValidationError

from practix_ugc_api.models.schemas import (
    RatingRequest,
    ReviewCreateRequest,
    ReviewSort,
    VoteRequest,
)
from practix_ugc_api.services.review_service import _ORDER_BY, _SELECT_REVIEWS

FILM_ID = uuid.uuid4()


@pytest.mark.parametrize('rating', [0, 5, 10])
def test_rating_accepts_the_whole_range(rating):
    assert RatingRequest(rating=rating).rating == rating


@pytest.mark.parametrize('rating', [-1, 11, 100])
def test_rating_rejects_out_of_range(rating):
    with pytest.raises(ValidationError):
        RatingRequest(rating=rating)


def test_rating_rejects_smuggled_user_id():
    with pytest.raises(ValidationError):
        RatingRequest(rating=9, user_id=str(uuid.uuid4()))


def test_review_rejects_smuggled_user_id():
    with pytest.raises(ValidationError):
        ReviewCreateRequest(film_id=FILM_ID, body='текст', user_id=str(uuid.uuid4()))


def test_review_body_must_not_be_empty():
    with pytest.raises(ValidationError):
        ReviewCreateRequest(film_id=FILM_ID, body='')


def test_review_body_is_bounded():
    with pytest.raises(ValidationError):
        ReviewCreateRequest(film_id=FILM_ID, body='х' * 100_000)


def test_review_author_rating_is_optional_but_bounded():
    assert ReviewCreateRequest(film_id=FILM_ID, body='текст').author_rating is None
    with pytest.raises(ValidationError):
        ReviewCreateRequest(film_id=FILM_ID, body='текст', author_rating=11)


@pytest.mark.parametrize('value', [0, 2, -2])
def test_vote_accepts_only_plus_and_minus_one(value):
    with pytest.raises(ValidationError):
        VoteRequest(value=value)


@pytest.mark.parametrize('raw', ['created_at', 'useful', "'; DROP TABLE reviews; --"])
def test_unknown_sort_is_rejected(raw):
    if raw in {sort.value for sort in ReviewSort}:
        pytest.skip('значение входит в белый список')
    with pytest.raises(ValueError, match='is not a valid ReviewSort'):
        ReviewSort(raw)


def test_every_sort_option_has_a_prepared_statement():
    """Белый список и набор готовых запросов обязаны совпадать.

    Расхождение означало бы KeyError на запросе с валидным `sort` — или, хуже,
    соблазн собрать недостающий запрос конкатенацией.
    """
    assert set(ReviewSort) == set(_ORDER_BY) == set(_SELECT_REVIEWS)


def test_sort_columns_are_real_review_columns():
    from practix_ugc_api.models.entity import Review

    columns = set(Review.__table__.columns.keys())
    for order in _ORDER_BY.values():
        assert order.split()[0] in columns
