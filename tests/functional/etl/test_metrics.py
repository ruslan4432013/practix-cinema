"""Мониторинг памяти и наблюдаемость.

Задание требует «внедрить средства мониторинга памяти приложения», поэтому
наличие этих метрик — такое же требование, как и доставка данных, и должно
ломаться в CI, если кто-то их выключит.
"""

import uuid

import helpers

MEMORY_METRICS = (
    # RSS процесса — то, по чему видно плато или монотонный рост.
    'process_resident_memory_bytes',
    # Живые блоки CPython: не зависят от поведения аллокатора.
    'etl_ch_allocated_blocks',
    'python_gc_collections_total',
    'python_gc_objects_collected_total',
)

PIPELINE_METRICS = (
    'etl_ch_messages_consumed_total',
    'etl_ch_rows_inserted_total',
    'etl_ch_clickhouse_up',
    'etl_ch_kafka_up',
    'etl_ch_paused_partitions',
    'etl_ch_consumer_lag_total',
    'etl_ch_insert_duration_seconds_bucket',
    'etl_ch_batch_rows_bucket',
)


class TestMemoryMonitoring:
    async def test_memory_metrics_are_exposed(self, etl_metrics):
        payload = etl_metrics()
        missing = [name for name in MEMORY_METRICS if name not in payload]
        assert not missing, f'нет метрик памяти: {missing}'

    async def test_rss_is_a_plausible_positive_value(self, metric_value):
        rss = metric_value('process_resident_memory_bytes')
        assert rss > 0
        # Верхняя граница — грубая защита от «метрика есть, но врёт»: ETL с
        # ограниченным буфером не может занимать гигабайты.
        assert rss < 2 * 1024**3

    async def test_tracemalloc_metrics_are_filled_when_enabled(self, metric_value):
        """В тестовом стенде ETL поднят с ETL_TRACEMALLOC_ENABLED=True."""
        assert metric_value('etl_ch_tracemalloc_traced_bytes') > 0
        assert metric_value('etl_ch_tracemalloc_peak_bytes') > 0


class TestPipelineMetrics:
    async def test_pipeline_metrics_are_exposed(self, etl_metrics):
        payload = etl_metrics()
        missing = [name for name in PIPELINE_METRICS if name not in payload]
        assert not missing, f'нет метрик конвейера: {missing}'

    async def test_counters_move_when_data_flows(self, send_event, wait_for, metric_value):
        before = metric_value('etl_ch_rows_inserted_total')

        film_id = str(uuid.uuid4())
        await send_event(helpers.video_progress(film_id=film_id))
        await wait_for(
            f"SELECT count() FROM ugc.film_views WHERE film_id = '{film_id}'",
            lambda result: result[0][0] == 1,
        )

        assert metric_value('etl_ch_rows_inserted_total') > before
        assert metric_value('etl_ch_clickhouse_up') == 1
        assert metric_value('etl_ch_kafka_up') == 1
