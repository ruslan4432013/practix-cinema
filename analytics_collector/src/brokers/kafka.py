"""Продюсер Kafka на базе aiokafka.

Три решения, которые определяют поведение модуля.

**1. Отправка без ожидания подтверждения.** На горячем пути используется
``send()``, а не ``send_and_wait()``: первый только кладёт запись в батч
продюсера и возвращает future, второй ждёт подтверждения от кворума реплик
(десятки миллисекунд). Для аналитики это правильный размен: ingest-ручка
обязана отвечать быстро, а сохранность обеспечивается не ожиданием, а
колбэком — при неудачной доставке запись уходит в буфер деградации.

**2. Ограниченное ожидание места в буфере.** Если брокер встал, буфер продюсера
заполняется, и ``send()`` начинает ждать. Без ограничения это выливается в
бесконечное накопление корутин и исчерпание памяти воркера. Поэтому вызов
обёрнут в ``asyncio.wait_for`` — по истечении ``KAFKA_MAX_BLOCK_MS`` запись
считается непринятой и уходит в буфер деградации.

**3. Сервис поднимается даже без Kafka.** Недоступность брокера на старте не
мешает запуску: продюсер переходит в degraded-режим, события копятся в буфере,
а фоновая задача продолжает попытки подключения. Иначе перезапуск кластера
Kafka означал бы недоступность приёма событий на всё время рестарта.

**4. Отказ записи и отказ брокера — разные вещи.** Неудача доставки может быть
свойством конкретного сообщения (слишком большое, недопустимый топик), а не
кластера. Раньше любая такая ошибка переводила продюсер в degraded, и одна
«отравленная» запись гнала весь поток в Redis при полностью живой Kafka.
Теперь ошибки уровня записи не трогают здоровье брокера — см.
``_RECORD_LEVEL_ERRORS``.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable

from aiokafka import AIOKafkaProducer
from aiokafka.errors import (
    CorruptRecordException,
    InvalidTimestampError,
    InvalidTopicError,
    KafkaError,
    MessageSizeTooLargeError,
    RecordListTooLargeError,
    RecordTooLargeError,
    UnsupportedForMessageFormatError,
)

from brokers.base import BrokerUnavailableError, EventBroker, RecordRejectedError
from core import metrics
from core.config import settings
from models.events import KafkaRecord

logger = logging.getLogger(__name__)

# Колбэк неудачной доставки: принимает запись, возвращает True, если сумел её
# сохранить (в буфер деградации), и False, если запись потеряна.
DeliveryFailureHandler = Callable[[KafkaRecord], Awaitable[bool]]

# ОШИБКИ УРОВНЯ ЗАПИСИ. Причина каждой — само сообщение, и повтор его же в
# здоровый кластер даст ровно тот же результат. Их принципиальное отличие от
# сетевых отказов в том, что они НИЧЕГО не говорят о доступности брокера:
# пометить его нездоровым из-за одной такой записи значит без нужды увести весь
# поток событий в буфер деградации при работающей Kafka.
#
# Запись при этом не теряется. Наружу летит RecordRejectedError — подкласс
# BrokerUnavailableError, поэтому горячий путь приёма ведёт себя как прежде
# (событие уходит в буфер, клиент получает 202), а дренаж буфера видит разницу
# и отправляет запись прямо в DLQ, не тратя на неё попытки. Раньше DLQ был
# недостижим вовсе: _mark_unhealthy сбрасывал is_healthy, а условие ухода в DLQ
# требует живого брокера — отравленная запись крутилась в буфере вечно.
_RECORD_LEVEL_ERRORS = (
    # Запись больше max_request_size продюсера или max.message.bytes топика.
    RecordTooLargeError,
    MessageSizeTooLargeError,
    RecordListTooLargeError,
    # Недопустимое имя топика: конфигурация, а не авария.
    InvalidTopicError,
    InvalidTimestampError,
    UnsupportedForMessageFormatError,
    CorruptRecordException,
)


def _is_record_level(error: BaseException) -> bool:
    """Отказ вызван самой записью, а не состоянием кластера."""
    return isinstance(error, _RECORD_LEVEL_ERRORS)


class KafkaEventBroker(EventBroker):
    """Публикация событий в Kafka с деградацией вместо отказа."""

    def __init__(self, on_delivery_failure: DeliveryFailureHandler | None = None):
        self._producer: AIOKafkaProducer | None = None
        self._healthy = False
        self._stopped = False
        self._on_delivery_failure = on_delivery_failure
        self._reconnect_task: asyncio.Task | None = None
        # Ссылки на фоновые задачи обязательно удерживать: asyncio хранит только
        # слабые ссылки на задачи, и без этого множества задача может быть
        # собрана сборщиком мусора до завершения (см. документацию create_task).
        self._pending_tasks: set[asyncio.Task] = set()
        self._start_lock = asyncio.Lock()

    def set_delivery_failure_handler(self, handler: DeliveryFailureHandler) -> None:
        """Назначает обработчик недоставленных записей.

        Позднее связывание нужно из-за взаимной зависимости: буферу деградации
        при создании требуется брокер (он публикует записи при дренаже), а
        брокеру — буфер (он принимает недоставленные записи).
        """
        self._on_delivery_failure = handler

    # ------------------------------------------------------------------ жизненный цикл

    async def start(self) -> None:
        """Пытается подключиться к Kafka и запускает фоновое переподключение."""
        self._stopped = False
        await self._try_connect()
        if self._reconnect_task is None:
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def stop(self) -> None:
        self._stopped = True
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reconnect_task
            self._reconnect_task = None

        # Даём уже отправленным записям шанс завершить доставку до закрытия.
        pending = list(self._pending_tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        if self._producer is not None:
            try:
                await self._producer.stop()
            except Exception as exc:  # noqa: BLE001 — на остановке ошибки только логируем
                logger.warning('Error while stopping Kafka producer: %s', exc)
            self._producer = None
        self._healthy = False
        metrics.broker_up.set(0)

    async def _try_connect(self) -> bool:
        async with self._start_lock:
            if self._healthy and self._producer is not None:
                return True

            # Продюсер от прошлой (неудачной) жизни нужно закрыть, иначе при
            # каждой попытке переподключения оставались бы висящие соединения
            # и фоновые задачи sender'а.
            if self._producer is not None:
                with contextlib.suppress(Exception):
                    await self._producer.stop()
                self._producer = None

            producer = AIOKafkaProducer(
                bootstrap_servers=settings.kafka_bootstrap_list,
                client_id=settings.KAFKA_CLIENT_ID,
                # acks='all' + идемпотентность: запись подтверждается только
                # после репликации на min.insync.replicas узлов, а повторы
                # продюсера при сетевых сбоях не порождают дубликатов.
                acks=settings.KAFKA_ACKS,
                enable_idempotence=settings.KAFKA_ENABLE_IDEMPOTENCE,
                compression_type=settings.KAFKA_COMPRESSION_TYPE,
                linger_ms=settings.KAFKA_LINGER_MS,
                max_batch_size=settings.KAFKA_MAX_BATCH_SIZE,
                request_timeout_ms=settings.KAFKA_REQUEST_TIMEOUT_MS,
            )
            try:
                await asyncio.wait_for(producer.start(), timeout=settings.KAFKA_MAX_BLOCK_MS / 1000)
            except (TimeoutError, KafkaError, OSError) as exc:
                logger.warning('Kafka producer is unavailable, running in degraded mode: %s', exc)
                # Освобождаем ресурсы недозапущенного продюсера.
                with contextlib.suppress(Exception):
                    await producer.stop()
                self._producer = None
                self._healthy = False
                metrics.broker_up.set(0)
                return False

            self._producer = producer
            self._healthy = True
            metrics.broker_up.set(1)
            logger.info('Kafka producer connected to %s', settings.KAFKA_BOOTSTRAP_SERVERS)
            return True

    async def _reconnect_loop(self) -> None:
        """Следит за состоянием брокера, пока сервис работает.

        Цикл делает две вещи: переподключается, когда брокер лежит, и активно
        проверяет связь, когда считает его живым. Вторая часть существенна:
        сервис работает в несколько воркеров, у каждого свой продюсер, и без
        активной проверки воркер, который в момент аварии не отправлял событий,
        продолжал бы считать Kafka доступной. Тогда ``/health/ready`` показывал
        бы «всё хорошо» в зависимости от того, какому воркеру достался запрос.
        """
        while not self._stopped:
            try:
                await asyncio.sleep(settings.KAFKA_RECONNECT_INTERVAL)
                if self._healthy:
                    await self._probe()
                else:
                    await self._try_connect()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — цикл не должен умирать
                logger.warning('Kafka health cycle failed: %s', exc)

    async def _probe(self) -> None:
        """Проверяет доступность кластера запросом метаданных."""
        producer = self._producer
        if producer is None:
            self._healthy = False
            metrics.broker_up.set(0)
            return
        try:
            await asyncio.wait_for(
                producer.client.fetch_all_metadata(),
                timeout=settings.KAFKA_MAX_BLOCK_MS / 1000,
            )
        except (TimeoutError, KafkaError, OSError) as exc:
            self._mark_unhealthy(exc)

    # ------------------------------------------------------------------ публикация

    @property
    def is_healthy(self) -> bool:
        return self._healthy and self._producer is not None

    async def publish(self, record: KafkaRecord) -> None:
        producer = self._producer
        if producer is None or not self._healthy:
            raise BrokerUnavailableError('Kafka producer is not connected')

        started = time.perf_counter()
        try:
            future = await asyncio.wait_for(
                producer.send(
                    record.topic,
                    value=record.value,
                    key=record.key.encode('utf-8'),
                    headers=record.headers,
                ),
                timeout=settings.KAFKA_MAX_BLOCK_MS / 1000,
            )
        except (TimeoutError, KafkaError, OSError) as exc:
            # Буфер продюсера полон или соединение потеряно — помечаем брокер
            # нездоровым, чтобы следующие события сразу шли в буфер деградации,
            # не тратя на ожидание по секунде каждое. Отказ же самой записи
            # (например, RecordTooLargeError на send) о брокере не говорит
            # ничего, и здоровье при нём не трогаем.
            if _is_record_level(exc):
                self._note_record_rejected(record, exc)
                raise RecordRejectedError(str(exc)) from exc
            self._mark_unhealthy(exc)
            raise BrokerUnavailableError(str(exc)) from exc
        finally:
            metrics.publish_duration.labels(topic=record.topic).observe(time.perf_counter() - started)

        future.add_done_callback(lambda fut: self._handle_delivery_result(fut, record))

    async def publish_now(self, record: KafkaRecord) -> None:
        producer = self._producer
        if producer is None or not self._healthy:
            raise BrokerUnavailableError('Kafka producer is not connected')
        try:
            await producer.send_and_wait(
                record.topic,
                value=record.value,
                key=record.key.encode('utf-8'),
                headers=record.headers,
            )
        except (TimeoutError, KafkaError, OSError) as exc:
            if _is_record_level(exc):
                # Сюда попадает дренаж буфера на отравленной записи. Здоровье
                # брокера не трогаем, а отдельным типом исключения говорим
                # дренажу, что повторять эту запись незачем — ей место в DLQ.
                self._note_record_rejected(record, exc)
                raise RecordRejectedError(str(exc)) from exc
            self._mark_unhealthy(exc)
            raise BrokerUnavailableError(str(exc)) from exc

    def _mark_unhealthy(self, exc: Exception) -> None:
        """Отмечает недоступность КЛАСТЕРА (не отдельной записи)."""
        if self._healthy:
            logger.warning('Kafka producer marked as degraded: %s', exc)
        self._healthy = False
        metrics.broker_up.set(0)

    @staticmethod
    def _note_record_rejected(record: KafkaRecord, error: BaseException) -> None:
        """Фиксирует отказ, относящийся к конкретной записи."""
        metrics.records_rejected.labels(topic=record.topic, error=type(error).__name__).inc()
        logger.error(
            'Kafka rejected a record (record-level error, broker health untouched): topic=%s error=%s',
            record.topic,
            error,
        )

    def _handle_delivery_result(self, future: asyncio.Future, record: KafkaRecord) -> None:
        """Обрабатывает подтверждение (или отказ) доставки от брокера.

        Вызывается из event loop, поэтому не должен блокировать: вся работа по
        сохранению записи выносится в отдельную задачу.
        """
        if future.cancelled():
            error: BaseException | None = asyncio.CancelledError()
        else:
            error = future.exception()
        if error is None:
            return

        metrics.delivery_failures.labels(topic=record.topic).inc()
        if _is_record_level(error):
            # Отравленная запись не является поводом объявить кластер лежащим:
            # иначе одно такое сообщение уводило бы весь поток в Redis до
            # следующего успешного _probe.
            self._note_record_rejected(record, error)
        else:
            logger.warning(
                'Kafka delivery failed for topic %s, moving record to fallback buffer: %s',
                record.topic,
                error,
            )
            self._mark_unhealthy(error if isinstance(error, Exception) else RuntimeError(str(error)))

        if self._on_delivery_failure is None:
            return
        task = asyncio.create_task(self._safe_failure_handler(record))
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def _safe_failure_handler(self, record: KafkaRecord) -> None:
        try:
            await self._on_delivery_failure(record)
        except Exception as exc:  # noqa: BLE001 — обработчик отказа сам падать не должен
            logger.error('Failed to buffer undelivered record: %s', exc)
