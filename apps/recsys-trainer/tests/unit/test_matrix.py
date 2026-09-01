"""Сборка матрицы взаимодействий: веса, фильтры, детерминизм."""

import datetime

import pytest

from practix_recsys_trainer.dataset.matrix import RATING_NEUTRAL, build, rating_multiplier
from practix_recsys_trainer.sources.clickhouse import Interaction

NOW = datetime.datetime(2026, 8, 30, 12, 0, tzinfo=datetime.UTC)


def view(user: str, film: str, weight: float = 1.0, offset_days: int = 0) -> Interaction:
    return Interaction(user, film, weight, NOW - datetime.timedelta(days=offset_days))


def test_missing_rating_leaves_view_weight_untouched():
    assert rating_multiplier(None) == 1.0


def test_neutral_rating_changes_nothing():
    assert rating_multiplier(int(RATING_NEUTRAL)) == pytest.approx(1.0)


def test_high_rating_strengthens_and_low_rating_weakens():
    """Оценка усиливает или гасит просмотр, но не создаёт его.

    Множителем, а не слагаемым: вес просмотра нормирован на [0, 1], и добавка
    обязана быть пропорциональной. Иначе брошенный на пятой минуте фильм с
    десяткой обогнал бы досмотренный без оценки — то есть явный сигнал подменил
    бы неявный, вопреки ADR-005.
    """
    assert rating_multiplier(10) == pytest.approx(1.5)
    assert rating_multiplier(0) == pytest.approx(0.5)

    abandoned_but_loved = 0.05 * rating_multiplier(10)
    finished_without_rating = 0.95 * rating_multiplier(None)
    assert abandoned_but_loved < finished_without_rating


def test_rating_is_applied_to_the_matching_pair_only():
    built = build(
        [view('u1', 'fA', 0.8), view('u1', 'fB', 0.8), view('u2', 'fA', 0.8), view('u2', 'fB', 0.8)],
        {('u1', 'fA'): 10},
        min_user_events=2,
        min_film_events=2,
    )
    dense = built.matrix.toarray()
    users, films = built.user_index, built.film_index

    assert dense[users['u1']][films['fA']] == pytest.approx(0.8 * 1.5)
    assert dense[users['u1']][films['fB']] == pytest.approx(0.8)


def test_duplicate_pairs_collapse_by_max_not_by_sum():
    """Вес — доля просмотра, и складывать её по сеансам нельзя.

    Человек, бросивший фильм и вернувшийся досмотреть, посмотрел его один раз
    целиком. Сумма дала бы «посмотрел на 130%», а среднее — «наполовину».
    """
    built = build(
        [
            view('u1', 'fA', 0.3),
            view('u1', 'fA', 1.0),
            view('u1', 'fB', 0.9),
            view('u2', 'fA', 0.5),
            view('u2', 'fB', 0.5),
        ],
        {},
        min_user_events=2,
        min_film_events=2,
    )
    dense = built.matrix.toarray()
    assert dense[built.user_index['u1']][built.film_index['fA']] == pytest.approx(1.0)


def test_rare_users_and_films_are_pruned_iteratively():
    """Один проход фильтра оставил бы строки, которые он сам объявил бесполезными.

    u3 смотрел только fZ, а fZ смотрел только u3. Отсечь редкий фильм — и u3
    становится пустым; отсечь u3 — и fZ становится пустым. Один проход убрал бы
    что-то одно.
    """
    built = build(
        [view('u1', 'fA'), view('u1', 'fB'), view('u2', 'fA'), view('u2', 'fB'), view('u3', 'fZ')],
        {},
        min_user_events=2,
        min_film_events=2,
    )

    assert built.user_ids == ['u1', 'u2']
    assert built.film_ids == ['fA', 'fB']


def test_everything_pruned_gives_an_empty_matrix_not_a_crash():
    """Пустой стенд — штатный исход прогона, а не аварийный."""
    built = build([view('u1', 'fA')], {}, min_user_events=2, min_film_events=2)

    assert built.shape == (0, 0)
    assert built.nnz == 0
    assert built.film_ids == []


def test_index_order_is_stable_across_builds():
    interactions = [view('u2', 'fB'), view('u1', 'fA'), view('u1', 'fB'), view('u2', 'fA')]

    first = build(interactions, {}, min_user_events=2, min_film_events=2)
    second = build(list(reversed(interactions)), {}, min_user_events=2, min_film_events=2)

    # Идентификаторы сортируются, а не берутся в порядке появления: иначе
    # порядок выдачи ClickHouse менял бы нумерацию строк и столбцов, а с ней —
    # и всё, что на неё опирается.
    assert first.user_ids == second.user_ids == ['u1', 'u2']
    assert first.film_ids == second.film_ids == ['fA', 'fB']
