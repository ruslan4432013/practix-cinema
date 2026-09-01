"""Сквозной прогон обучения против живых ClickHouse и витрины.

Главное, что здесь проверяется, — три системных требования, которые нельзя
проверить юнит-тестом, потому что они про поведение СУБД: идемпотентность
повторного прогона (F0.1), целостность переключения версии (F0.2) и то, что
батч действительно читает ту таблицу, из которой потом учится.
"""

import uuid

from sqlalchemy import text

from practix_recsys_trainer.core.config import settings
from practix_recsys_trainer.pipeline import run_training


def shelf_state(engine) -> dict:
    with engine.connect() as conn:
        pointer = conn.execute(
            text(
                'SELECT v.version, v.status, v.models FROM shelf_pointer p '
                'JOIN shelf_version v ON v.version = p.version WHERE p.id = true'
            )
        ).first()
        # Считаем строки ЖИВОЙ версии, а не всей таблицы. Идемпотентность
        # означает «та же витрина», а не «столько же строк в базе»: прошлые
        # версии живут рядом до уборки, и суммарный счётчик закономерно растёт,
        # ничего при этом не удваивая внутри версии.
        live = pointer.version if pointer is not None else -1
        counts = {
            table: conn.execute(
                text(f'SELECT count(*) FROM {table} WHERE version = :v'),
                {'v': live},
            ).scalar_one()
            for table in ('similar_item', 'personal_item', 'popular_item', 'catalog_film')
        }
        versions = conn.execute(text('SELECT count(*) FROM shelf_version')).scalar_one()
    return {'pointer': pointer, 'counts': counts, 'versions': versions}


def test_training_publishes_a_ready_version_and_moves_the_pointer(shelf_engine, seed_views):
    seed_views()

    result = run_training(run_key='test:first', with_metrics=False)

    assert not result.skipped
    state = shelf_state(shelf_engine)
    assert state['pointer'].version == result.version
    # Указатель встаёт ТОЛЬКО на готовую версию: иначе выдача, читающая
    # состояние с фильтром по статусу, увидела бы «витрины нет».
    assert state['pointer'].status == 'ready'
    assert state['counts']['similar_item'] > 0
    assert state['counts']['popular_item'] > 0
    assert state['counts']['catalog_film'] > 0


def test_similar_films_stay_inside_their_viewing_group(shelf_engine, seed_views):
    """Модель обязана находить со-просмотры, а не выдавать шум.

    Две группы зрителей смотрят непересекающиеся половины каталога. Если сосед
    фильма из первой половины окажется из второй, значит близость посчитана не
    по общим зрителям, и вся фича бессмысленна.
    """
    groups = seed_views()
    run_training(run_key='test:groups', with_metrics=False)

    group_a = {uuid.UUID(film_id) for film_id in groups['group_a']}
    with shelf_engine.connect() as conn:
        rows = conn.execute(
            text('SELECT film_id, rec_film_id FROM similar_item WHERE film_id = ANY(:films)'),
            {'films': list(group_a)},
        ).all()

    assert rows, 'у фильмов первой группы обязаны быть соседи'
    assert all(row.rec_film_id in group_a for row in rows)
    # F2.3: исходный фильм в свою же выдачу не попадает.
    assert all(row.film_id != row.rec_film_id for row in rows)


def test_rerun_on_the_same_data_is_idempotent(shelf_engine, seed_views):
    """F0.1: повторный запуск даёт то же состояние витрины и не удваивает записи.

    Идемпотентность здесь не «мы аккуратно написали ON CONFLICT», а следствие
    схемы: версия входит в первичный ключ каждой таблицы списков, поэтому
    второй прогон физически пишет в другое место.
    """
    seed_views()
    first = run_training(run_key='test:run-1', with_metrics=False)
    before = shelf_state(shelf_engine)

    second = run_training(run_key='test:run-2', with_metrics=False)
    after = shelf_state(shelf_engine)

    assert second.version != first.version
    assert after['pointer'].version == second.version
    # Строк столько же, а не вдвое больше: они принадлежат разным версиям.
    assert after['counts'] == before['counts']


def test_the_same_run_key_never_builds_a_second_shelf(shelf_engine, seed_views):
    """Уникальность run_key — это то, что стоит между вторым тиком планировщика
    и второй витриной на тех же данных."""
    seed_views()
    first = run_training(run_key='test:same-slot', with_metrics=False)

    repeat = run_training(run_key='test:same-slot', with_metrics=False)

    assert repeat.skipped
    assert repeat.version is None
    assert shelf_state(shelf_engine)['pointer'].version == first.version


def test_old_versions_are_pruned_but_never_the_live_one(shelf_engine, seed_views):
    seed_views()
    for index in range(settings.RECS_TRAINER_KEEP_VERSIONS + 2):
        run_training(run_key=f'test:prune-{index}', with_metrics=False)

    state = shelf_state(shelf_engine)

    assert state['versions'] <= settings.RECS_TRAINER_KEEP_VERSIONS
    # Актуальная версия обязана пережить уборку — иначе выдача осталась бы с
    # указателем на удалённую строку. Защита двойная: явное исключение в
    # запросе и ON DELETE RESTRICT в схеме.
    assert state['pointer'] is not None
    with shelf_engine.connect() as conn:
        live_rows = conn.execute(
            text('SELECT count(*) FROM popular_item WHERE version = :v'),
            {'v': state['pointer'].version},
        ).scalar_one()
    assert live_rows > 0


def test_empty_clickhouse_still_publishes_a_usable_shelf(shelf_engine, ch_client, catalog):
    """Пустой стенд — штатный исход, а не авария.

    Витрина без каталога оставила бы выдачу без возможности отличить
    несуществующий фильм от фильма без соседей, а прогон, падающий на пустых
    данных, означал бы, что первый же запуск стенда — инцидент.
    """
    result = run_training(run_key='test:empty', with_metrics=False)

    assert not result.skipped
    state = shelf_state(shelf_engine)
    assert state['pointer'].status == 'ready'
    assert state['counts']['catalog_film'] == len(catalog)
    assert state['counts']['similar_item'] == 0


def test_personal_recommendations_exclude_what_the_user_already_watched(shelf_engine, seed_views):
    """F4.3: рекомендовать вчера досмотренный фильм — заметнее всего показать,
    что рекомендаций нет.

    История сверяется с той, которую построила фикстура, а не с повторным
    запросом в ClickHouse: иначе тест проверял бы модель против результата ещё
    одного чтения, а не против того, что ей скормили.
    """
    seeded = seed_views()
    run_training(run_key='test:personal', with_metrics=False)

    with shelf_engine.connect() as conn:
        rows = conn.execute(text('SELECT user_id, film_id FROM personal_item')).all()

    if not rows:
        # ALS может не дать ни строки на вырожденной выборке — это допустимо
        # (эпик режется первым), и тогда проверять нечего.
        return

    watched = {uuid.UUID(user): {uuid.UUID(film) for film in films} for user, films in seeded['watched'].items()}
    for row in rows:
        assert row.film_id not in watched.get(row.user_id, set()), (
            f'пользователю {row.user_id} рекомендован уже просмотренный фильм {row.film_id}'
        )
