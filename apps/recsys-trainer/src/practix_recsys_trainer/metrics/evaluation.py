"""Оценка качества на отложенной во времени выборке (E5).

Три выдачи считаются ОДНИМ И ТЕМ ЖЕ кодом на ОДНОЙ И ТОЙ ЖЕ выборке:

* ``popular`` — baseline «просто популярное». Фиксируется первым (F3.2). Он
  сильнее, чем кажется: на любом каталоге заметная доля просмотров приходится
  на верхушку, и модель, не обогнавшая его, не заработала своей сложности;
* ``cooccurrence`` — соседи того, что человек уже смотрел;
* ``als`` — матричная факторизация.

МОДЕЛИ ОБУЧАЮТСЯ ЗАНОВО, НА ОБУЧАЮЩЕЙ ЧАСТИ. Взять готовую витрину было бы
проще и неверно: она построена на ВСЕХ данных, включая тестовые, и метрики
показали бы, насколько хорошо модель помнит ответы, а не насколько хорошо
предсказывает.

Из выдачи исключается то, что пользователь смотрел в обучающей части: иначе
модель набирала бы очки, «предсказывая» уже известное.
"""

import logging

from practix_recsys_trainer.core.config import settings
from practix_recsys_trainer.dataset import matrix as matrix_builder
from practix_recsys_trainer.dataset.split import group_by_user, split_by_time
from practix_recsys_trainer.metrics.quality import QualityReport, evaluate
from practix_recsys_trainer.models import als, cooccurrence, popular
from practix_recsys_trainer.sources.clickhouse import Interaction

logger = logging.getLogger(__name__)

DEFAULT_K = 10


def evaluate_models(
    interactions: list[Interaction],
    ratings: dict[tuple[str, str], int] | None = None,
    *,
    k: int = DEFAULT_K,
    catalog_size: int = 0,
) -> list[QualityReport]:
    """Таблица сравнения baseline и моделей. Пустой список — мерить нечего."""
    split = split_by_time(interactions)
    if not split.is_usable:
        logger.warning('METRICS SKIPPED: отложенная выборка пуста, мерить нечего')
        return []

    train_matrix = matrix_builder.build(
        split.train,
        ratings or {},
        min_user_events=settings.RECS_TRAINER_MIN_USER_EVENTS,
        min_film_events=settings.RECS_TRAINER_MIN_FILM_EVENTS,
    )
    if train_matrix.nnz == 0:
        logger.warning('METRICS SKIPPED: обучающая выборка после фильтров пуста')
        return []

    seen = group_by_user(split.train)
    # Релевантно только то, чего человек ещё не видел в обучающей части:
    # «угадать» уже просмотренное — не предсказание, а память.
    relevant = {user_id: films - seen.get(user_id, set()) for user_id, films in group_by_user(split.test).items()}
    relevant = {user_id: films for user_id, films in relevant.items() if films}
    if not relevant:
        logger.warning('METRICS SKIPPED: в отложенной выборке нет новых для пользователей фильмов')
        return []

    catalog_size = catalog_size or train_matrix.shape[1]
    popular_top = popular.from_matrix(train_matrix.matrix, train_matrix.film_ids, limit=k)
    popular_films = [film_id for film_id, _ in popular_top]

    reports = [
        evaluate(
            'popular (baseline)',
            {user_id: [film for film in popular_films if film not in seen.get(user_id, set())] for user_id in relevant},
            relevant,
            k=k,
            catalog_size=catalog_size,
        ),
        evaluate(
            'cooccurrence',
            _cooccurrence_recommendations(train_matrix, seen, relevant, k=k),
            relevant,
            k=k,
            catalog_size=catalog_size,
        ),
    ]

    if settings.RECS_TRAINER_ALS_ENABLED:
        reports.append(
            evaluate('als', _als_recommendations(train_matrix, relevant, k=k), relevant, k=k, catalog_size=catalog_size)
        )
    return reports


def _cooccurrence_recommendations(
    train_matrix: matrix_builder.InteractionMatrix,
    seen: dict[str, set[str]],
    relevant: dict[str, set[str]],
    *,
    k: int,
) -> dict[str, list[str]]:
    """Персональная выдача из item-to-item: складываем соседей всего просмотренного.

    Это и есть способ, которым блок «похожие» превращается в персональную
    подборку без отдельной модели: близости соседей суммируются по истории
    пользователя. Суммирование, а не максимум: фильм, оказавшийся соседом
    сразу нескольких просмотренных, релевантнее того, кто похож на один.
    """
    neighbours = cooccurrence.build_similar(train_matrix.matrix, top_n=settings.RECS_TRAINER_TOP_N)
    film_index = train_matrix.film_index
    film_ids = train_matrix.film_ids

    recommendations: dict[str, list[str]] = {}
    for user_id in relevant:
        watched = seen.get(user_id, set())
        scores: dict[int, float] = {}
        for film_id in watched:
            index = film_index.get(film_id)
            if index is None:
                continue
            for neighbour, score in neighbours.get(index, []):
                if film_ids[neighbour] in watched:
                    continue
                scores[neighbour] = scores.get(neighbour, 0.0) + score
        if scores:
            ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:k]
            recommendations[user_id] = [film_ids[index] for index, _ in ordered]
    return recommendations


def _als_recommendations(
    train_matrix: matrix_builder.InteractionMatrix,
    relevant: dict[str, set[str]],
    *,
    k: int,
) -> dict[str, list[str]]:
    try:
        raw = als.train_and_recommend(
            train_matrix.matrix,
            factors=settings.RECS_TRAINER_ALS_FACTORS,
            iterations=settings.RECS_TRAINER_ALS_ITERATIONS,
            regularization=settings.RECS_TRAINER_ALS_REGULARIZATION,
            alpha=settings.RECS_TRAINER_ALS_ALPHA,
            top_n=k,
            seed=settings.RECS_TRAINER_ALS_SEED,
            max_users=settings.RECS_TRAINER_MAX_PERSONAL_USERS,
        )
    except Exception as exc:  # noqa: BLE001 — отсутствие строки в таблице честнее падения всей оценки
        logger.warning('ALS METRICS FAILED: %s', exc)
        return {}

    user_ids = train_matrix.user_ids
    film_ids = train_matrix.film_ids
    recommendations = {user_ids[user]: [film_ids[film] for film, _ in items] for user, items in raw.items()}
    return {user_id: films for user_id, films in recommendations.items() if user_id in relevant}
