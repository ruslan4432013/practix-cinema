"""Прогрев горячего слоя и метрики качества на живых данных."""

import json

import redis
from sqlalchemy import text

from practix_recsys_trainer.core.config import settings
from practix_recsys_trainer.metrics.evaluation import evaluate_models
from practix_recsys_trainer.metrics.quality import format_table
from practix_recsys_trainer.pipeline import run_training
from practix_recsys_trainer.shelf.warmup import POINTER_KEY
from practix_recsys_trainer.sources import clickhouse


def redis_client() -> redis.Redis:
    return redis.Redis(
        host=settings.RECS_REDIS_HOST,
        port=settings.RECS_REDIS_PORT,
        db=settings.RECS_REDIS_DB,
        decode_responses=True,
    )


def test_pointer_is_written_after_the_lists_not_before(shelf_engine, seed_views):
    """ADR-004: указатель выставляется последним.

    Обратный порядок оставил бы окно, в котором выдача видит новый номер версии
    и идёт за ключами, которых ещё нет: весь трафик проваливался бы в
    PostgreSQL сразу после переключения — то есть плановая операция сама себе
    создавала бы пик.
    """
    client = redis_client()
    client.flushdb()
    seed_views()

    result = run_training(run_key='test:warmup', with_metrics=False)

    assert client.get(POINTER_KEY) == str(result.version)
    # Исход прогрева обязан быть виден в результате прогона, а не только в
    # логе: прогон, у которого кэш не сложился, завершается успешно и красит
    # все метрики свежести в зелёное (F0.4).
    assert result.warmed is True
    assert result.stats['warmed'] is True
    popular = client.get(f'recs:v{result.version}:popular')
    assert popular is not None
    assert json.loads(popular)

    # У ключей версии есть TTL — именно он убирает старую витрину после
    # переключения, вместо массового удаления ключей.
    assert client.ttl(f'recs:v{result.version}:popular') > 0
    # А у указателя TTL нет: истёкший указатель означал бы поход в базу за
    # номером версии на каждом запросе.
    assert client.ttl(POINTER_KEY) == -1
    client.close()


def test_warmed_similar_keys_match_what_the_shelf_holds(shelf_engine, seed_views):
    client = redis_client()
    client.flushdb()
    seed_views()

    result = run_training(run_key='test:warmup-similar', with_metrics=False)

    with shelf_engine.connect() as conn:
        row = conn.execute(
            text(
                'SELECT film_id, rec_film_id, score FROM similar_item WHERE version = :v ORDER BY film_id, rank LIMIT 1'
            ),
            {'v': result.version},
        ).first()

    cached = client.get(f'recs:v{result.version}:similar:{row.film_id}')
    assert cached is not None
    assert json.loads(cached)[0][0] == str(row.rec_film_id)
    client.close()


def test_quality_table_puts_the_baseline_next_to_the_models(shelf_engine, seed_views):
    """F3.2 и F3.3: baseline фиксируется и печатается вместе с моделями.

    Сравнивать модель саму с собой бессмысленно — без строки «просто
    популярное» таблица не отвечает на вопрос, ради которого её считают.
    """
    seed_views()
    client = clickhouse.connect()
    try:
        interactions = clickhouse.fetch_interactions(client)
    finally:
        client.close()

    reports = evaluate_models(interactions, {}, k=5, catalog_size=60)

    assert reports, 'на посеянных данных метрики обязаны считаться'
    names = [report.name for report in reports]
    assert names[0] == 'popular (baseline)'
    assert 'cooccurrence' in names

    table = format_table(reports)
    assert 'precision@k' in table
    assert 'покрытие' in table


def test_training_stores_quality_and_coverage_in_the_version(shelf_engine, seed_views):
    """Метрики кладутся тем же прогоном, что их посчитал.

    Иначе «какие метрики были у этой версии» пришлось бы восстанавливать по
    времени запуска соседней команды.
    """
    seed_views()

    result = run_training(run_key='test:stats', with_metrics=True)

    with shelf_engine.connect() as conn:
        stats = conn.execute(
            text('SELECT stats FROM shelf_version WHERE version = :v'), {'v': result.version}
        ).scalar_one()

    assert stats['rows']['similar'] > 0
    assert 'catalog_coverage' in stats
    assert stats['duration_seconds'] >= 0


def test_shelf_state_carries_the_stats_of_the_live_version(shelf_engine, seed_views):
    """Метрики описывают ВИТРИНУ, а не процесс, который её посчитал.

    На стенде обучение часто запускают отдельной командой, в другом процессе.
    Гейдж покрытия, заполняемый только собственным прогоном демона, показывал бы
    ноль при живой и полной витрине, и алерт RecsCatalogCoverageLow срабатывал бы
    на пустом месте — а алерт, который врёт, перестают читать.
    """
    from practix_recsys_trainer.shelf import writer

    seed_views()
    result = run_training(run_key='test:state', with_metrics=False)

    with shelf_engine.connect() as conn:
        version, finished_at, stats = writer.read_state(conn)

    assert version == result.version
    assert finished_at is not None
    assert stats['catalog_coverage'] > 0
    assert stats['rows']['similar'] > 0
