"""Пайплайн обучения: извлечение → матрица → модели → витрина → прогрев.

Это тот самый «офлайн, который думает» из ADR-001. Всё тяжёлое происходит
здесь, чтобы в момент запроса не происходило ничего: выдача только читает
разложенное. Разрез проходит по витрине, и он же — единственный контракт между
этим модулем и ``recommendations-api``.

ПОРЯДОК ШАГОВ ВЫБРАН ТАК, ЧТОБЫ ЧАСТИЧНЫЙ УСПЕХ ОСТАВАЛСЯ УСПЕХОМ. Популярное
считается первым и от моделей не зависит: даже если и со-встречаемость, и ALS
не дадут ни строки, версия опубликуется с работающей последней ступенью
деградации. Обратный порядок (сначала модели) при их отказе оставил бы выдачу
вовсе без витрины.

ALS ИЗОЛИРОВАН ОТ ОСТАЛЬНОГО. Он режется первым по плану, поэтому его отказ
обязан стоить только персональной выдачи, а не всего прогона: исключение
ловится, пишется в статистику, версия публикуется без ``personal_item``.
"""

import datetime
import logging
import time
from dataclasses import dataclass, field

from practix_recsys_trainer.core.config import settings
from practix_recsys_trainer.dataset import matrix as matrix_builder
from practix_recsys_trainer.metrics import quality
from practix_recsys_trainer.metrics.evaluation import evaluate_models
from practix_recsys_trainer.models import als, cooccurrence, popular
from practix_recsys_trainer.shelf import warmup, writer
from practix_recsys_trainer.sources import catalog, clickhouse, ratings

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TrainingResult:
    version: int | None
    run_key: str
    models: str
    rows: dict[str, int] = field(default_factory=dict)
    stats: dict = field(default_factory=dict)
    skipped: bool = False
    reports: list[quality.QualityReport] = field(default_factory=list)


def build_run_key(moment: datetime.datetime | None = None) -> str:
    """Естественный ключ прогона — час, в который он запущен.

    Час, а не минута и не UUID: ключ обязан быть ОДИНАКОВЫМ у двух попыток
    одного и того же слота расписания, иначе повторный тик (или вторая реплика,
    которой быть не должно, но которая однажды окажется) построит вторую
    витрину на тех же данных. С UUID уникальность была бы, а идемпотентности —
    нет.
    """
    moment = moment or datetime.datetime.now(datetime.UTC)
    return f'train:{moment.strftime("%Y-%m-%dT%H")}'


def run_training(*, run_key: str | None = None, with_metrics: bool = True) -> TrainingResult:
    """Полный прогон. Возвращает результат; исключения наружу не выпускает молча."""
    started = time.monotonic()
    run_key = run_key or build_run_key()
    models_used: list[str] = []

    logger.info('Прогон %s начат', run_key)

    client = clickhouse.connect()
    try:
        interactions = clickhouse.fetch_interactions(client)
        popular_rows = clickhouse.fetch_popular(
            client,
            limit=settings.RECS_TRAINER_POPULAR_LIMIT,
            window_days=settings.RECS_TRAINER_POPULAR_WINDOW_DAYS,
        )
    finally:
        client.close()

    logger.info('Взаимодействий получено: %s, популярных фильмов: %s', len(interactions), len(popular_rows))

    explicit = ratings.fetch_ratings()
    interaction_matrix = matrix_builder.build(
        interactions,
        explicit,
        min_user_events=settings.RECS_TRAINER_MIN_USER_EVENTS,
        min_film_events=settings.RECS_TRAINER_MIN_FILM_EVENTS,
    )
    users, films = interaction_matrix.shape
    logger.info('Матрица: %s пользователей × %s фильмов, ненулевых %s', users, films, interaction_matrix.nnz)

    if not popular_rows:
        # Запасной путь: отдельный запрос за популярным не удался или окно
        # пусто, но матрица прочитана. Витрина без популярного оставила бы
        # выдачу без последней ступени лестницы деградации.
        popular_rows = popular.from_matrix(
            interaction_matrix.matrix,
            interaction_matrix.film_ids,
            limit=settings.RECS_TRAINER_POPULAR_LIMIT,
        )
        logger.info('Популярное посчитано по матрице: %s позиций', len(popular_rows))

    similar_rows = _build_similar(interaction_matrix)
    if similar_rows:
        models_used.append('cooccurrence')
        logger.info('Со-встречаемость: соседи у %s фильмов', len({row[0] for row in similar_rows}))

    personal_rows = _build_personal(interaction_matrix)
    if personal_rows:
        models_used.append('als')
        logger.info('ALS: персональная выдача у %s пользователей', len({row[0] for row in personal_rows}))

    if popular_rows:
        models_used.append('popular')

    catalog_ids = catalog.fetch_film_ids()
    logger.info('Каталог: %s фильмов', len(catalog_ids))

    reports: list[quality.QualityReport] = []
    if with_metrics:
        reports = evaluate_models(interactions, explicit, catalog_size=len(catalog_ids) or films)

    return _publish(
        run_key=run_key,
        models=','.join(models_used),
        similar_rows=similar_rows,
        personal_rows=personal_rows,
        popular_rows=popular_rows,
        catalog_ids=catalog_ids,
        interaction_matrix=interaction_matrix,
        reports=reports,
        elapsed=time.monotonic() - started,
    )


def _build_similar(interaction_matrix: matrix_builder.InteractionMatrix) -> list[tuple[str, int, str, float]]:
    neighbours = cooccurrence.build_similar(
        interaction_matrix.matrix,
        top_n=settings.RECS_TRAINER_TOP_N,
    )
    film_ids = interaction_matrix.film_ids
    return [
        (film_ids[film], rank, film_ids[neighbour], score)
        for film, items in neighbours.items()
        for rank, (neighbour, score) in enumerate(items)
    ]


def _build_personal(interaction_matrix: matrix_builder.InteractionMatrix) -> list[tuple[str, int, str, float]]:
    if not settings.RECS_TRAINER_ALS_ENABLED or interaction_matrix.nnz == 0:
        return []
    try:
        recommendations = als.train_and_recommend(
            interaction_matrix.matrix,
            factors=settings.RECS_TRAINER_ALS_FACTORS,
            iterations=settings.RECS_TRAINER_ALS_ITERATIONS,
            regularization=settings.RECS_TRAINER_ALS_REGULARIZATION,
            alpha=settings.RECS_TRAINER_ALS_ALPHA,
            top_n=settings.RECS_TRAINER_TOP_N,
            seed=settings.RECS_TRAINER_ALS_SEED,
            max_users=settings.RECS_TRAINER_MAX_PERSONAL_USERS,
        )
    except Exception as exc:  # noqa: BLE001 — E6 режется первым: его отказ стоит только персональной выдачи
        logger.warning('ALS FAILED: версия выйдет без персональной выдачи (%s)', exc)
        return []

    user_ids = interaction_matrix.user_ids
    film_ids = interaction_matrix.film_ids
    return [
        (user_ids[user], rank, film_ids[film], score)
        for user, items in recommendations.items()
        for rank, (film, score) in enumerate(items)
    ]


def _publish(
    *,
    run_key: str,
    models: str,
    similar_rows: list[tuple[str, int, str, float]],
    personal_rows: list[tuple[str, int, str, float]],
    popular_rows: list[tuple[str, float]],
    catalog_ids: list[str],
    interaction_matrix: matrix_builder.InteractionMatrix,
    reports: list[quality.QualityReport],
    elapsed: float,
) -> TrainingResult:
    engine = writer.make_engine()
    try:
        with engine.begin() as conn, writer.writer_lock(conn):
            try:
                version = writer.open_version(conn, run_key, models)
            except writer.VersionAlreadyBuilt:
                # Не ошибка: этот слот расписания уже отработан. Ровно так
                # выглядит второй тик планировщика после перезапуска.
                logger.info('Прогон %s пропущен: версия уже построена', run_key)
                return TrainingResult(version=None, run_key=run_key, models=models, skipped=True)

            rows = {
                'similar': writer.write_similar(conn, version, similar_rows),
                'personal': writer.write_personal(conn, version, personal_rows),
                'popular': writer.write_popular(
                    conn, version, [(rank, film_id, score) for rank, (film_id, score) in enumerate(popular_rows)]
                ),
                'catalog': writer.write_catalog(conn, version, catalog_ids),
            }

            users, films = interaction_matrix.shape
            covered = len({row[0] for row in similar_rows})
            stats = {
                'rows': rows,
                'matrix': {'users': users, 'films': films, 'interactions': interaction_matrix.nnz},
                'catalog_size': len(catalog_ids),
                # Покрытие каталога — SLO из раздела 5.1: ниже 60% половина
                # каталога уходит в популярное, и фича перестаёт быть фичей.
                'catalog_coverage': round(covered / len(catalog_ids), 4) if catalog_ids else 0.0,
                'duration_seconds': round(elapsed, 2),
                'quality': [report.as_row() for report in reports],
            }
            writer.publish(conn, version, stats)
            pruned = writer.prune_versions(conn, settings.RECS_TRAINER_KEEP_VERSIONS)
            stats['pruned_versions'] = pruned
    finally:
        engine.dispose()

    logger.info('Версия %s опубликована: %s', version, stats['rows'])

    # Прогрев ПОСЛЕ коммита. Внутри транзакции он разложил бы в кэш версию,
    # которой при откате никогда не существовало бы, и выдача пошла бы за
    # номером версии, отсутствующим в базе.
    warmup.warm(
        version,
        popular=popular_rows,
        similar=_top_similar_for_warmup(similar_rows),
    )

    return TrainingResult(
        version=version, run_key=run_key, models=models, rows=stats['rows'], stats=stats, reports=reports
    )


def _top_similar_for_warmup(rows: list[tuple[str, int, str, float]]) -> dict[str, list[tuple[str, float]]]:
    """Соседи по фильмам в форме, которую кладёт в кэш выдача."""
    grouped: dict[str, list[tuple[str, float]]] = {}
    for film_id, _, rec_film_id, score in rows:
        grouped.setdefault(film_id, []).append((rec_film_id, score))
    return grouped
