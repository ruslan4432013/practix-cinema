"""Матрица «пользователь × фильм» в разреженном виде.

Плотная матрица здесь невозможна арифметически, а не «неоптимальна»: 200 000
пользователей × 10 000 фильмов float32 — это 8 ГБ на объект, из которых
заполнены доли процента. CSR хранит только ненулевые элементы, и вся матрица
стенда помещается в десятки мегабайт.

ВЕС СОБИРАЕТСЯ ЗДЕСЬ, И ЭТО ЕДИНСТВЕННОЕ МЕСТО, где просмотр и оценка
складываются в одно число. Формула держится в чистой функции без ввода-вывода
именно поэтому: её легко проверить юнит-тестом, а «где-то в SQL, где-то в
питоне» — это два места, которые разъедутся.

Фильтры по минимальной активности применяются ИТЕРАТИВНО. Отсечение редких
фильмов делает часть пользователей односкладочными, а отсечение таких
пользователей делает редкими новые фильмы; один проход оставил бы в матрице
строки и столбцы, которые сам же фильтр объявил бесполезными.
"""

from dataclasses import dataclass

import numpy as np
from scipy import sparse

from practix_recsys_trainer.sources.clickhouse import Interaction

# Оценка сдвигает вес просмотра в диапазоне [-0.5, +0.5]: десятка усиливает
# сигнал в полтора раза, ноль — почти гасит. Умножением, а не сложением: вес
# просмотра уже нормирован на [0, 1], и добавка должна быть пропорциональной,
# иначе брошенный на пятой минуте фильм с высокой оценкой обгонял бы
# досмотренный без оценки.
RATING_NEUTRAL = 5.0
RATING_INFLUENCE = 0.1


def rating_multiplier(rating: int | None) -> float:
    """Во сколько раз оценка меняет вес просмотра. Нет оценки — не меняет."""
    if rating is None:
        return 1.0
    return 1.0 + (rating - RATING_NEUTRAL) * RATING_INFLUENCE


@dataclass(frozen=True, slots=True)
class InteractionMatrix:
    """CSR-матрица плюс карты индексов в обе стороны."""

    matrix: sparse.csr_matrix
    user_ids: list[str]
    film_ids: list[str]

    @property
    def user_index(self) -> dict[str, int]:
        return {user_id: idx for idx, user_id in enumerate(self.user_ids)}

    @property
    def film_index(self) -> dict[str, int]:
        return {film_id: idx for idx, film_id in enumerate(self.film_ids)}

    @property
    def shape(self) -> tuple[int, int]:
        return self.matrix.shape

    @property
    def nnz(self) -> int:
        return int(self.matrix.nnz)


def _prune(
    rows: list[tuple[str, str, float]],
    *,
    min_user_events: int,
    min_film_events: int,
) -> list[tuple[str, str, float]]:
    """Итеративно выбрасывает слишком редких пользователей и фильмы."""
    current = rows
    while True:
        user_counts: dict[str, int] = {}
        film_counts: dict[str, int] = {}
        for user_id, film_id, _ in current:
            user_counts[user_id] = user_counts.get(user_id, 0) + 1
            film_counts[film_id] = film_counts.get(film_id, 0) + 1

        kept = [
            row for row in current if user_counts[row[0]] >= min_user_events and film_counts[row[1]] >= min_film_events
        ]
        if len(kept) == len(current):
            return kept
        if not kept:
            return []
        current = kept


def build(
    interactions: list[Interaction],
    ratings: dict[tuple[str, str], int] | None = None,
    *,
    min_user_events: int = 2,
    min_film_events: int = 2,
) -> InteractionMatrix:
    """Список взаимодействий в CSR-матрицу весов.

    Дубли пары «пользователь-фильм» складываются функцией ``max``, а не суммой:
    вес — это доля просмотра, и суммировать её по сеансам значило бы получить
    «посмотрел на 300%». ClickHouse уже сворачивает пары через ``max``, но
    сборка не имеет права зависеть от того, что вызывающий об этом помнил.
    """
    ratings = ratings or {}
    weighted = [
        (
            item.user_id,
            item.film_id,
            item.weight * rating_multiplier(ratings.get((item.user_id, item.film_id))),
        )
        for item in interactions
    ]
    kept = _prune(weighted, min_user_events=min_user_events, min_film_events=min_film_events)
    if not kept:
        return InteractionMatrix(sparse.csr_matrix((0, 0), dtype=np.float32), [], [])

    user_ids = sorted({row[0] for row in kept})
    film_ids = sorted({row[1] for row in kept})
    user_index = {user_id: idx for idx, user_id in enumerate(user_ids)}
    film_index = {film_id: idx for idx, film_id in enumerate(film_ids)}

    best: dict[tuple[int, int], float] = {}
    for user_id, film_id, weight in kept:
        key = (user_index[user_id], film_index[film_id])
        if weight > best.get(key, 0.0):
            best[key] = weight

    rows = np.fromiter((key[0] for key in best), dtype=np.int32, count=len(best))
    cols = np.fromiter((key[1] for key in best), dtype=np.int32, count=len(best))
    values = np.fromiter(best.values(), dtype=np.float32, count=len(best))

    matrix = sparse.csr_matrix(
        (values, (rows, cols)),
        shape=(len(user_ids), len(film_ids)),
        dtype=np.float32,
    )
    return InteractionMatrix(matrix=matrix, user_ids=user_ids, film_ids=film_ids)
