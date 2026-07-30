"""Устойчивость к отказу ХРАНИЛИЩА — главное требование задания.

Проверяется не «сервис не упал», а гораздо более сильное утверждение: пока
ClickHouse недоступен, оффсеты в Kafka НЕ ДВИГАЮТСЯ. Именно это означает
«данные не потеряны»: если бы ETL коммитил прочитанное до записи, авария
хранилища превратилась бы в дыру в аналитике, которую нечем закрыть.

Отказ имитируется через toxiproxy, а не `docker compose stop`: не нужен
docker-сокет в контейнере тестов и нет зависимости от имени compose-проекта.
"""

import asyncio
import os
import uuid

import helpers
from aiokafka.admin import AIOKafkaAdminClient
from aiokafka.structs import TopicPartition

KAFKA_BOOTSTRAP = os.environ.get('KAFKA_BOOTSTRAP_SERVERS', 'kafka:9092')
GROUP_ID = os.environ.get('ETL_CONSUMER_GROUP', 'ugc-clickhouse-etl-test')
PROGRESS_TOPIC = os.environ.get('KAFKA_TOPIC_VIDEO_PROGRESS', 'ugc.video_progress.v1')

EVENTS_DURING_OUTAGE = 50


async def _committed_offsets(topic: str) -> dict[int, int]:
    """Прокоммиченные оффсеты группы ETL по партициям топика."""
    admin = AIOKafkaAdminClient(bootstrap_servers=KAFKA_BOOTSTRAP.split(','))
    await admin.start()
    try:
        offsets = await admin.list_consumer_group_offsets(GROUP_ID)
    finally:
        await admin.close()
    return {
        tp.partition: meta.offset
        for tp, meta in offsets.items()
        if isinstance(tp, TopicPartition) and tp.topic == topic and meta.offset >= 0
    }


async def _wait_metric(metric_value, name: str, predicate, timeout: float = 60.0) -> float:
    deadline = asyncio.get_running_loop().time() + timeout
    last = float('nan')
    while asyncio.get_running_loop().time() < deadline:
        last = metric_value(name)
        if predicate(last):
            return last
        await asyncio.sleep(0.5)
    raise AssertionError(f'metric {name} did not satisfy the condition in {timeout}s, last value: {last}')


class TestStorageOutage:
    async def test_no_data_is_lost_while_clickhouse_is_down(
        self,
        ch,
        send_event,
        wait_for,
        metric_value,
        toxiproxy,
    ):
        film_id = str(uuid.uuid4())

        # Прогреваем конвейер: убеждаемся, что до аварии всё работает и группа
        # успела зафиксировать позицию.
        warmup = helpers.video_progress(film_id=film_id, position_ms=0)
        await send_event(warmup)
        await wait_for(
            f"SELECT count() FROM ugc.film_views WHERE film_id = '{film_id}'",
            lambda result: result[0][0] == 1,
        )
        await _wait_metric(metric_value, 'etl_ch_clickhouse_up', lambda value: value == 1)
        offsets_before = await _committed_offsets(PROGRESS_TOPIC)

        # --- Хранилище падает ---
        toxiproxy.break_storage()
        for index in range(EVENTS_DURING_OUTAGE):
            await send_event(
                helpers.video_progress(
                    film_id=film_id,
                    position_ms=(index + 1) * 2_000,
                    duration_ms=120_000,
                )
            )

        # ETL обязан заметить отказ и включить backpressure, а не копить в памяти.
        await _wait_metric(metric_value, 'etl_ch_clickhouse_up', lambda value: value == 0)
        await _wait_metric(metric_value, 'etl_ch_paused_partitions', lambda value: value > 0)

        # ГЛАВНОЕ УТВЕРЖДЕНИЕ ТЕСТА: оффсеты не сдвинулись, значит события
        # по-прежнему лежат в Kafka и будут перечитаны.
        await asyncio.sleep(5)
        offsets_during = await _committed_offsets(PROGRESS_TOPIC)
        for partition, offset in offsets_during.items():
            assert offset <= offsets_before.get(partition, offset), (
                f'оффсет партиции {partition} сдвинулся при недоступном ClickHouse — '
                f'события между этим оффсетом и записью потеряны'
            )

        # --- Хранилище возвращается ---
        toxiproxy.heal_storage()

        rows = await wait_for(
            f"SELECT uniq(event_id) FROM ugc.film_views WHERE film_id = '{film_id}'",
            lambda result: result[0][0] == EVENTS_DURING_OUTAGE + 1,
            timeout=120,
        )
        assert rows[0][0] == EVENTS_DURING_OUTAGE + 1, 'все события, отправленные во время аварии, доехали'

        await _wait_metric(metric_value, 'etl_ch_clickhouse_up', lambda value: value == 1)
        await _wait_metric(metric_value, 'etl_ch_paused_partitions', lambda value: value == 0)
