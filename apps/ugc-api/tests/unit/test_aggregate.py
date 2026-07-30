"""Арифметика преагрегата — единственная часть записи, проверяемая без базы.

Именно здесь исследование указывает на лёгкую ошибку: при изменении оценки
количество не меняется, а при повторе той же оценки не меняется вообще ничего,
и слепой `+1/-1` по двум одинаковым позициям гистограммы дал бы мусор.
"""

import pytest

from practix_ugc_api.services.aggregate import (
    HISTOGRAM_SIZE,
    average,
    rating_deltas,
    split_by_threshold,
    vote_deltas,
)


def test_new_rating_increments_count_sum_and_bucket():
    delta = rating_deltas(None, 9)
    assert (delta.count, delta.total) == (1, 9)
    assert delta.histogram[9] == 1
    assert sum(delta.histogram) == 1
    assert not delta.is_noop


def test_changed_rating_keeps_count_and_moves_two_buckets():
    delta = rating_deltas(3, 8)
    assert delta.count == 0, 'пользователь как оценивал фильм, так и оценивает — количество не меняется'
    assert delta.total == 5
    assert delta.histogram[3] == -1
    assert delta.histogram[8] == 1
    assert sum(delta.histogram) == 0


def test_same_rating_changes_nothing():
    delta = rating_deltas(7, 7)
    assert delta.is_noop
    assert delta.histogram == [0] * HISTOGRAM_SIZE


def test_deleted_rating_reverses_everything():
    delta = rating_deltas(4, None)
    assert (delta.count, delta.total) == (-1, -4)
    assert delta.histogram[4] == -1


def test_set_then_delete_is_identity():
    for rating in range(HISTOGRAM_SIZE):
        forward = rating_deltas(None, rating)
        backward = rating_deltas(rating, None)
        assert forward.count + backward.count == 0
        assert forward.total + backward.total == 0
        assert [a + b for a, b in zip(forward.histogram, backward.histogram, strict=True)] == [0] * HISTOGRAM_SIZE


@pytest.mark.parametrize('rating', [0, 10])
def test_boundaries_land_in_the_edge_buckets(rating):
    """Позиция в списке равна значению оценки.

    В SQL тот же список адресуется как ``hist[i]`` при 1-индексных массивах
    PostgreSQL, то есть индекс списка = значение оценки = SQL-индекс минус один.
    Оценка 10 обязана попасть в последнюю из одиннадцати позиций, а не за неё.
    """
    delta = rating_deltas(None, rating)
    assert len(delta.histogram) == HISTOGRAM_SIZE
    assert delta.histogram[rating] == 1


def test_no_rating_at_all_is_a_noop():
    assert rating_deltas(None, None).is_noop


# --- гистограмма ------------------------------------------------------------
HISTOGRAM = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]  # всего 66 оценок


def test_threshold_splits_histogram():
    likes, dislikes = split_by_threshold(HISTOGRAM, 6)
    assert likes == sum(HISTOGRAM[6:])
    assert dislikes == sum(HISTOGRAM[:6])
    assert likes + dislikes == sum(HISTOGRAM)


@pytest.mark.parametrize(
    ('threshold', 'expected'),
    [(0, (66, 0)), (HISTOGRAM_SIZE, (0, 66))],
)
def test_extreme_thresholds(threshold, expected):
    assert split_by_threshold(HISTOGRAM, threshold) == expected


def test_average_of_empty_film_is_none():
    assert average(0, 0) is None, '«средняя 0» и «оценок нет» — разные вещи'


def test_average_divides():
    assert average(66, 11) == 6.0


# --- голоса за рецензии -----------------------------------------------------
@pytest.mark.parametrize(
    ('old', 'new', 'expected'),
    [
        (None, 1, (1, 0, 1)),
        (None, -1, (0, 1, -1)),
        (1, 1, (0, 0, 0)),
        (1, -1, (-1, 1, -2)),
        (-1, 1, (1, -1, 2)),
        (1, None, (-1, 0, -1)),
        (-1, None, (0, -1, 1)),
    ],
)
def test_vote_deltas(old, new, expected):
    assert vote_deltas(old, new) == expected


@pytest.mark.parametrize('value', [1, -1])
def test_vote_and_retraction_cancel_out(value):
    forward = vote_deltas(None, value)
    backward = vote_deltas(value, None)
    assert [a + b for a, b in zip(forward, backward, strict=True)] == [0, 0, 0]


def test_useful_score_is_likes_minus_dislikes():
    for old in (None, 1, -1):
        for new in (None, 1, -1):
            likes, dislikes, useful = vote_deltas(old, new)
            assert useful == likes - dislikes
