"""ALS: единственное, что здесь проверяется, — исключение просмотренного (F4.3).

Качество факторизации юнит-тестом не проверить, и не нужно: за него отвечают
метрики E5 на отложенной выборке. А вот инвариант «просмотренного в выдаче нет»
— проверяемый, обязательный и, как выяснилось, не гарантированный библиотекой.
"""

import numpy as np
import pytest
from scipy import sparse

from practix_recsys_trainer.models.als import train_and_recommend

FACTORS = 8
ITERATIONS = 3


def two_group_matrix(users_per_group: int = 12, films_per_group: int = 6) -> sparse.csr_matrix:
    """Половина зрителей смотрит первую половину каталога, половина — вторую."""
    rows, cols = [], []
    for group in (0, 1):
        for user in range(users_per_group):
            for film in range(films_per_group):
                rows.append(group * users_per_group + user)
                cols.append(group * films_per_group + film)
    values = np.full(len(rows), 0.95, dtype=np.float32)
    return sparse.csr_matrix(
        (values, (rows, cols)),
        shape=(users_per_group * 2, films_per_group * 2),
    )


def recommend(matrix: sparse.csr_matrix, top_n: int) -> dict[int, list[tuple[int, float]]]:
    return train_and_recommend(
        matrix,
        factors=FACTORS,
        iterations=ITERATIONS,
        regularization=0.05,
        alpha=40.0,
        top_n=top_n,
        seed=42,
        max_users=1000,
    )


def test_watched_films_never_appear_in_the_answer():
    """Регрессия на поведение implicit, а не на собственную арифметику.

    Флаг ``filter_already_liked_items`` не держит слова: когда неотфильтрованных
    кандидатов меньше запрошенного N, библиотека ДОБИВАЕТ выдачу уже
    просмотренными позициями. На матрице 24×12 запрос десяти рекомендаций
    возвращал 6 новых и 4 просмотренных — то есть бил ровно по активным
    зрителям, по самой ценной аудитории.
    """
    matrix = two_group_matrix()

    # top_n БОЛЬШЕ, чем есть непросмотренных кандидатов (их ровно 6): именно на
    # этом соотношении библиотека и начинает добивать выдачу.
    result = recommend(matrix, top_n=10)

    assert result, 'ALS обязан что-то предложить на такой матрице'
    for user, positions in result.items():
        watched = set(matrix.indices[matrix.indptr[user] : matrix.indptr[user + 1]].tolist())
        recommended = {film for film, _ in positions}
        assert not (recommended & watched), f'пользователю {user} рекомендовано просмотренное'


def test_answer_is_truncated_to_top_n():
    result = recommend(two_group_matrix(), top_n=3)

    assert all(len(positions) <= 3 for positions in result.values())


def test_recommendations_come_from_the_other_group():
    """Проверка, что модель вообще чему-то научилась, а не отдала случайное."""
    matrix = two_group_matrix()

    result = recommend(matrix, top_n=6)

    # Пользователь 0 из первой группы: всё, что ему предложат, обязано лежать
    # во второй половине каталога — больше непросмотренного у него нет.
    assert all(film >= 6 for film, _ in result[0])


def test_scores_are_ordered_and_finite():
    result = recommend(two_group_matrix(), top_n=6)

    for positions in result.values():
        scores = [score for _, score in positions]
        assert scores == sorted(scores, reverse=True)
        assert all(np.isfinite(score) for score in scores)


def test_empty_matrix_yields_nothing_instead_of_raising():
    assert recommend(sparse.csr_matrix((0, 0), dtype=np.float32), top_n=5) == {}


@pytest.mark.parametrize('top_n', [1, 5, 20])
def test_no_watched_leak_at_any_depth(top_n):
    matrix = two_group_matrix()

    for user, positions in recommend(matrix, top_n=top_n).items():
        watched = set(matrix.indices[matrix.indptr[user] : matrix.indptr[user + 1]].tolist())
        assert not ({film for film, _ in positions} & watched)
