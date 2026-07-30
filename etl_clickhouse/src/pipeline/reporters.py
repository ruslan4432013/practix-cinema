"""Фоновые репортёры состояния.

Обе метрики нужны именно как активные замеры, а не как побочный эффект
основного цикла: пассивные показатели после аварии показывали бы «всё хорошо»
просто потому, что работы давно не было.
"""

import asyncio
import logging

from aiokafka.errors import KafkaError

from core import metrics
from core.config import settings

logger = logging.getLogger(__name__)


async def _sleep_until_stop(stop_event: asyncio.Event, timeout: float) -> bool:
    """Спит timeout секунд. Возвращает True, если пора останавливаться."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout)
        return True
    except TimeoutError:
        return False


async def report_lag(consumer, stop_event: asyncio.Event) -> None:
    """Отставание консьюмера по каждой партиции.

    Лаг — главный индикатор здоровья ETL: растущий лаг при живом ClickHouse
    означает, что мы не успеваем, а растущий лаг при мёртвом — что данные
    копятся в Kafka, то есть работает backpressure, а не теряются.
    """
    while not stop_event.is_set():
        try:
            total = 0
            for tp in consumer.assignment():
                highwater = consumer.highwater(tp)
                # До первой выборки из партиции highwater неизвестен — это
                # нормально сразу после ребаланса, а не ошибка.
                if highwater is None:
                    continue
                position = await consumer.position(tp)
                lag = max(0, highwater - position)
                metrics.consumer_lag.labels(topic=tp.topic, partition=str(tp.partition)).set(lag)
                total += lag
            metrics.consumer_lag_total.set(total)
        except (KafkaError, AssertionError) as exc:
            # AssertionError прилетает из aiokafka, если партицию отобрали
            # между assignment() и position().
            logger.debug('Lag report skipped: %s', exc)
        except Exception:
            logger.exception('Lag reporter iteration failed')

        if await _sleep_until_stop(stop_event, settings.ETL_LAG_INTERVAL):
            return


async def report_sink_health(sink, stop_event: asyncio.Event) -> None:
    """Активная проба доступности ClickHouse."""
    while not stop_event.is_set():
        try:
            metrics.clickhouse_up.set(1 if await sink.ping() else 0)
        except Exception:  # noqa: BLE001
            metrics.clickhouse_up.set(0)

        if await _sleep_until_stop(stop_event, settings.CH_HEALTH_INTERVAL):
            return
