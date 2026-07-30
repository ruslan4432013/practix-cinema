"""Консьюмер Kafka: создание и устойчивый старт."""

import asyncio
import logging

from aiokafka import AIOKafkaConsumer
from aiokafka.errors import KafkaError

from core import metrics
from core.config import settings

logger = logging.getLogger(__name__)


def build_consumer(listener) -> AIOKafkaConsumer:
    consumer = AIOKafkaConsumer(
        bootstrap_servers=settings.kafka_bootstrap_list,
        group_id=settings.ETL_CONSUMER_GROUP,
        client_id=settings.ETL_CLIENT_ID,
        # Оффсет коммитится ТОЛЬКО после успешной вставки в ClickHouse — это и
        # есть гарантия «хотя бы один раз». Автокоммит по таймеру подтверждал
        # бы факт чтения до факта записи, и падение процесса между ними
        # потеряло бы события безвозвратно.
        enable_auto_commit=False,
        auto_offset_reset=settings.ETL_AUTO_OFFSET_RESET,
        # Главные рычаги потребления памяти: пик RSS определяется буферами
        # выборки, а не размером пачки. getmany(max_records=N) ограничивает
        # только выдачу — фетчер к этому моменту уже сложил в память целые
        # ответы по всем назначенным партициям.
        max_partition_fetch_bytes=settings.ETL_MAX_PARTITION_FETCH_BYTES,
        fetch_max_bytes=settings.ETL_FETCH_MAX_BYTES,
        max_poll_interval_ms=settings.ETL_MAX_POLL_INTERVAL_MS,
        session_timeout_ms=settings.ETL_SESSION_TIMEOUT_MS,
    )
    # Подписка через subscribe(), а не через конструктор AIOKafkaConsumer(*topics):
    # только так можно передать ConsumerRebalanceListener, без которого ручной
    # коммит после ребаланса падает с CommitFailedError.
    consumer.subscribe(topics=settings.topics, listener=listener)
    return consumer


async def start_with_retry(consumer: AIOKafkaConsumer, stop_event: asyncio.Event) -> bool:
    """Ждёт Kafka вместо того, чтобы падать.

    Порядок старта контейнеров в compose гарантирует только что брокеры
    здоровы, но в реальной эксплуатации Kafka может быть недоступна и позже.
    Сервис, падающий на старте, превращает это в рестарт-луп; сервис,
    ожидающий её, — в задержку.

    Возвращает False, если во время ожидания пришёл сигнал остановки.
    """
    attempt = 0
    while not stop_event.is_set():
        try:
            await consumer.start()
            metrics.kafka_up.set(1)
            logger.info(
                'Kafka consumer started',
                extra={'group_id': settings.ETL_CONSUMER_GROUP, 'topics': settings.topics},
            )
            return True
        except (KafkaError, OSError) as exc:
            attempt += 1
            metrics.kafka_up.set(0)
            logger.warning(
                'Kafka is not available yet, retrying',
                extra={'error': str(exc), 'attempt': attempt, 'retry_in': settings.KAFKA_RECONNECT_INTERVAL},
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=settings.KAFKA_RECONNECT_INTERVAL)
            except TimeoutError:
                continue
    return False
