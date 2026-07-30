"""Сердце сервиса: цикл «прочитать — преобразовать — вставить — прокоммитить».

Три требования задания закрываются именно здесь.

**Устойчивость к сбою источника.** Три независимых механизма: старт при лежащей
Kafka (``start_with_retry``), перехват ``KafkaError`` внутри цикла чтения без
падения процесса, и ``FlushOnRevokeListener``, закрывающий пачку до отдачи
партиций.

**Устойчивость к сбою хранилища.** Оффсеты коммитятся ТОЛЬКО после успешной
вставки, а на время недоступности ClickHouse чтение из Kafka ставится на паузу.
Порядок «вставил -> прокоммитил» неразрывен: между этими шагами возможно
падение процесса, и тогда пачка вставится повторно, а её погасит дедупликация
ClickHouse. Обратный порядок дал бы потерю. Это осознанный выбор at-least-once
поверх недостижимого exactly-once.

**Непрерывность без утечки.** Память ограничена одной пачкой по построению
(см. ``pipeline/buffer.py``), а не надеждой, что хранилище скоро вернётся.
"""

import asyncio
import contextlib
import logging
import time

from aiokafka.errors import CommitFailedError, KafkaError

from practix_etl_clickhouse.core import metrics
from practix_etl_clickhouse.core.backoff import backoff_sleep
from practix_etl_clickhouse.core.config import settings
from practix_etl_clickhouse.core.tracing import get_tracer
from practix_etl_clickhouse.pipeline.buffer import BatchBuffer
from practix_etl_clickhouse.sinks.base import SinkSchemaError, SinkUnavailableError
from practix_etl_clickhouse.sinks.clickhouse import ClickHouseSink, build_dedup_token
from practix_etl_clickhouse.transform.film_views import FILM_VIEW_COLUMN_TYPES, FILM_VIEW_COLUMNS, FILM_VIEWS_TABLE
from practix_etl_clickhouse.transform.invalid import INVALID_COLUMN_TYPES, INVALID_COLUMNS, INVALID_TABLE
from practix_etl_clickhouse.transform.raw import RAW_COLUMN_TYPES, RAW_COLUMNS, RAW_TABLE

logger = logging.getLogger(__name__)


class FlushFailed(Exception):
    """Пачку не удалось записать за отведённое число попыток."""


class Runner:
    def __init__(self, consumer, sink: ClickHouseSink, buffer: BatchBuffer, stop_event: asyncio.Event) -> None:
        self._consumer = consumer
        self._sink = sink
        self._buffer = buffer
        self._stop = stop_event
        self._paused = False
        # Защита от рекурсии: «холостой» getmany внутри цикла ретраев может
        # спровоцировать ребаланс, а его колбэк попытается сделать флаш —
        # то есть войти во flush повторно.
        self._in_flush = False

    # --- Основной цикл ----------------------------------------------------

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                batches = await self._consumer.getmany(
                    timeout_ms=settings.ETL_POLL_TIMEOUT_MS,
                    max_records=settings.ETL_MAX_RECORDS,
                )
                metrics.kafka_up.set(1)
            except KafkaError as exc:
                # Отказ источника не имеет права убить процесс: Kafka вернётся,
                # а накопленная пачка и позиция чтения переживут её отсутствие.
                metrics.kafka_up.set(0)
                delay = await backoff_sleep(
                    0, settings.CH_RETRY_START_DELAY, settings.CH_RETRY_FACTOR, settings.CH_RETRY_MAX_DELAY
                )
                logger.warning('Kafka read failed, retrying', extra={'error': str(exc), 'retry_in': delay})
                continue

            for tp, messages in batches.items():
                for message in messages:
                    self._buffer.add(tp, message)

            reason = self._buffer.should_flush()
            if reason:
                await self.flush(reason)

        # Финальный флаш: то, что уже прочитано, должно доехать до хранилища,
        # иначе перезапуск заставил бы перечитывать его заново.
        await self.flush('shutdown')

    # --- Запись пачки -----------------------------------------------------

    async def flush(self, reason: str) -> None:
        if self._buffer.is_empty:
            # Пустую пачку не пишем, но таймер сбрасываем: иначе первое же
            # событие после простоя улетело бы отдельной вставкой на одну
            # строку.
            self._buffer.clear()
            return
        if self._in_flush:
            return

        self._in_flush = True
        rows = self._buffer.rows
        nbytes = self._buffer.nbytes
        # Спан на пачку, а не на сообщение: сообщений десятки тысяч в секунду,
        # и спан на каждое сам стал бы нагрузкой. Пачка — естественная единица
        # работы этого сервиса, и именно её длительность отвечает на вопрос
        # «почему событие доехало до хранилища через двадцать минут».
        # Связь с трейсом конкретного события даёт инструментация aiokafka:
        # она восстанавливает контекст из заголовков сообщения.
        with get_tracer().start_as_current_span('etl.flush') as span:
            span.set_attribute('etl.flush_reason', reason)
            span.set_attribute('etl.batch_rows', rows)
            span.set_attribute('etl.batch_bytes', nbytes)
            try:
                await self._insert_with_retry()
                await self._commit()
            finally:
                self._in_flush = False

        metrics.flush_total.labels(reason=reason).inc()
        metrics.batches_total.labels(result='ok').inc()
        metrics.batch_rows.observe(rows)
        metrics.batch_bytes.observe(nbytes)
        metrics.last_flush_timestamp.set(time.time())
        logger.info(
            'Batch flushed',
            extra={'reason': reason, 'rows': rows, 'bytes': nbytes},
        )
        self._buffer.clear()
        await self._resume()

    async def _insert_with_retry(self) -> None:
        attempt = 0
        while True:
            try:
                await self._insert_once()
                metrics.clickhouse_up.set(1)
                return
            except (SinkUnavailableError, SinkSchemaError) as exc:
                metrics.clickhouse_up.set(0)
                metrics.batches_total.labels(result='retry').inc()

                # Схемная ошибка повтором не лечится — это баг в DDL или в
                # трансформации. Но и коммитить пачку нельзя: тихо потерять
                # данные хуже, чем громко встать. Отсюда ERROR и остановка
                # конвейера, которую видно и в метрике, и в алерте по лагу.
                if isinstance(exc, SinkSchemaError):
                    logger.error('ClickHouse rejected the batch (schema)', extra={'error': str(exc)})
                else:
                    logger.warning('ClickHouse is unavailable', extra={'error': str(exc)})

                if settings.CH_INSERT_MAX_ATTEMPTS and attempt + 1 >= settings.CH_INSERT_MAX_ATTEMPTS:
                    metrics.batches_total.labels(result='failed').inc()
                    raise FlushFailed(str(exc)) from exc

                # BACKPRESSURE. Пока приёмник лежит, чтение из Kafka
                # остановлено — и память ограничена одной пачкой. Альтернатива
                # (копить непрокоммиченные пачки в списке) превращает часовой
                # простой ClickHouse в OOM-kill, после которого всё равно
                # пришлось бы перечитывать из Kafka с последнего коммита.
                await self._pause()

                delay = await backoff_sleep(
                    attempt,
                    settings.CH_RETRY_START_DELAY,
                    settings.CH_RETRY_FACTOR,
                    settings.CH_RETRY_MAX_DELAY,
                )
                attempt += 1
                logger.info('Retrying insert', extra={'attempt': attempt, 'slept': delay})

                # ОБЯЗАТЕЛЬНО. pause() не продлевает max_poll_interval_ms:
                # aiokafka отсчитывает интервал от последнего getmany. Если
                # просто спать в цикле ретраев, группа исключит консьюмера и
                # запустит ребаланс прямо посреди аварии. На паузе холостой
                # getmany мгновенно возвращает {} и обновляет дедлайн.
                await self._heartbeat()

                # Пересоздаём клиент, переключаясь на другого координатора:
                # отказ мог быть не общим, а конкретного узла.
                with contextlib.suppress(SinkUnavailableError):
                    await self._sink.reconnect()

    async def _insert_once(self) -> None:
        buffer = self._buffer
        # Порядок важен: сырая таблица первой. Если процесс упадёт между
        # вставками, film_views окажется неполной относительно raw_events, а
        # не наоборот — восстановить витрины из сырых данных можно, обратное
        # невозможно.
        if buffer.raw_rows:
            await self._sink.insert(
                RAW_TABLE,
                buffer.raw_rows,
                RAW_COLUMNS,
                RAW_COLUMN_TYPES,
                build_dedup_token(RAW_TABLE, buffer.ranges),
            )
        if buffer.view_rows:
            await self._sink.insert(
                FILM_VIEWS_TABLE,
                buffer.view_rows,
                FILM_VIEW_COLUMNS,
                FILM_VIEW_COLUMN_TYPES,
                build_dedup_token(FILM_VIEWS_TABLE, buffer.ranges),
            )
        if buffer.invalid_rows:
            await self._sink.insert(
                INVALID_TABLE,
                buffer.invalid_rows,
                INVALID_COLUMNS,
                INVALID_COLUMN_TYPES,
                build_dedup_token(INVALID_TABLE, buffer.ranges),
            )

    async def _commit(self) -> None:
        if not self._buffer.offsets:
            return
        try:
            await self._consumer.commit(self._buffer.offsets)
        except CommitFailedError as exc:
            # Партиции уже у другого консьюмера. Данные не потеряны — он
            # перечитает их с прошлого коммита, а дубликаты погасит
            # ReplacingMergeTree.
            metrics.commit_failures.inc()
            logger.warning('Offset commit failed after rebalance', extra={'error': str(exc)})

    # --- Пауза и возобновление --------------------------------------------

    async def _pause(self) -> None:
        if self._paused:
            return
        assignment = self._consumer.assignment()
        if not assignment:
            return
        self._consumer.pause(*assignment)
        self._paused = True
        metrics.paused_partitions.set(len(assignment))
        logger.warning('Consumption paused (backpressure)', extra={'partitions': len(assignment)})

    async def _resume(self) -> None:
        if not self._paused:
            return
        assignment = self._consumer.assignment()
        if assignment:
            self._consumer.resume(*assignment)
        self._paused = False
        metrics.paused_partitions.set(0)
        logger.info('Consumption resumed')

    async def _heartbeat(self) -> None:
        """Холостой poll: подтверждает группе, что консьюмер жив."""
        try:
            await self._consumer.getmany(timeout_ms=0)
        except KafkaError as exc:
            logger.debug('Heartbeat poll failed: %s', exc)

    # --- Колбэки ребаланса ------------------------------------------------

    async def on_revoke(self) -> None:
        """Закрыть пачку до отдачи партиций.

        Если приёмник недоступен, буфер выбрасывается без коммита: держать
        ребаланс до возвращения ClickHouse значило бы гарантированно вылететь
        из группы. Потери нет — события перечитает новый владелец партиций.
        """
        if self._in_flush or self._buffer.is_empty:
            self._buffer.clear()
            return
        try:
            self._in_flush = True
            await self._insert_once()
            await self._commit()
            metrics.flush_total.labels(reason='revoke').inc()
        except (SinkUnavailableError, SinkSchemaError) as exc:
            metrics.batches_total.labels(result='dropped').inc()
            logger.warning(
                'Dropping uncommitted batch on rebalance: it will be re-read by the new owner',
                extra={'error': str(exc), 'rows': self._buffer.rows},
            )
        finally:
            self._in_flush = False
            self._buffer.clear()

    async def on_assign(self, assigned) -> None:
        """Начать с чистого листа на новом наборе партиций."""
        self._buffer.clear()
        # Состояние паузы не переносится на вновь назначенные партиции —
        # сбрасываем флаг, иначе _resume() посчитал бы, что возобновлять
        # нечего, и чтение осталось бы остановленным навсегда.
        self._paused = False
        metrics.paused_partitions.set(0)
        # Ряды лага по отобранным партициям иначе застыли бы навсегда на
        # последнем значении и наврали бы в алертах.
        metrics.consumer_lag.clear()
