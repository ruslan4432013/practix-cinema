"""Арифметика со-встречаемости на матрицах, посчитанных руками.

Матрицы здесь маленькие намеренно: ответ должен быть проверяем глазами, иначе
тест проверяет не модель, а то, что модель не изменилась.
"""

import numpy as np
import pytest
from scipy import sparse

from practix_recsys_trainer.models.cooccurrence import build_similar


def csr(rows: list[list[float]]) -> sparse.csr_matrix:
    return sparse.csr_matrix(np.array(rows, dtype=np.float32))


def test_two_clusters_do_not_leak_into_each_other():
    # Двое смотрят f0 и f1, двое других — f2 и f3. Пересечения аудиторий нет,
    # значит и соседей между кластерами быть не должно.
    matrix = csr([[1, 1, 0, 0], [1, 1, 0, 0], [0, 0, 1, 1], [0, 0, 1, 1]])

    result = build_similar(matrix, top_n=3)

    assert [film for film, _ in result[0]] == [1]
    assert [film for film, _ in result[2]] == [3]
    assert result[0][0][1] == pytest.approx(1.0, abs=1e-6)


def test_film_is_never_similar_to_itself():
    """F2.3: исходный фильм в выдачу не попадает.

    Без явного обнуления диагонали он занимал бы первую позицию у каждого
    фильма — косинус с самим собой равен единице, — и блок из десяти позиций
    отдавал бы девять полезных.
    """
    matrix = csr([[1, 1, 1], [1, 1, 0], [1, 0, 1]])

    result = build_similar(matrix, top_n=5)

    for film, neighbours in result.items():
        assert film not in [neighbour for neighbour, _ in neighbours]


def test_popular_film_does_not_become_similar_to_everything():
    """Нормировка косинуса — единственное, что мешает популярному стать соседом всем.

    f0 смотрят все, f1 и f2 — по паре человек из разных половин. Без деления на
    длины столбцов близость свелась бы к числу общих зрителей, и f0 оказался бы
    ближайшим соседом каждого фильма каталога.
    """
    matrix = csr(
        [
            [1, 1, 0],
            [1, 1, 0],
            [1, 0, 1],
            [1, 0, 1],
        ]
    )

    result = build_similar(matrix, top_n=3)

    # У f1 и f2 общих зрителей нет вовсе, поэтому единственный сосед — f0,
    # но его близость строго меньше единицы: аудитории совпадают лишь наполовину.
    assert [film for film, _ in result[1]] == [0]
    assert result[1][0][1] < 1.0


def test_top_n_truncates_and_orders_by_score():
    matrix = csr([[1, 1, 1, 1], [1, 1, 1, 0], [1, 1, 0, 0]])

    result = build_similar(matrix, top_n=2)

    for neighbours in result.values():
        assert len(neighbours) <= 2
        scores = [score for _, score in neighbours]
        assert scores == sorted(scores, reverse=True)


def test_ties_are_broken_by_film_index_so_runs_are_reproducible():
    """Детерминизм заявлен как свойство и обязан держаться не на удаче.

    При равных близостях порядок иначе зависел бы от внутреннего порядка
    argpartition, и два прогона на одних данных давали бы разные витрины.
    """
    matrix = csr([[1, 1, 1, 1], [1, 1, 1, 1]])

    first = build_similar(matrix, top_n=2)
    second = build_similar(matrix, top_n=2)

    assert first == second
    assert [film for film, _ in first[0]] == [1, 2]


def test_block_size_does_not_change_the_answer():
    """Блочный проход — предохранитель по памяти, а не другая модель."""
    matrix = csr([[1, 1, 0, 1], [1, 0, 1, 1], [0, 1, 1, 0], [1, 1, 1, 1]])

    assert build_similar(matrix, top_n=3, block_size=1) == build_similar(matrix, top_n=3, block_size=64)


def test_empty_matrix_yields_no_neighbours():
    assert build_similar(sparse.csr_matrix((0, 0), dtype=np.float32), top_n=5) == {}
