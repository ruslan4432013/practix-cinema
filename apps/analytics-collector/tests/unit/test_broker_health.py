"""Классификация отказов продюсера Kafka (``brokers/kafka.py``).

Смысл разделения: одна «отравленная» запись не должна объявлять весь кластер
лежащим. Если бы объявляла, каждое такое сообщение уводило бы ВЕСЬ поток
событий в Redis-буфер до следующего успешного ``_probe`` — при полностью
работающей Kafka.

Тесты синхронные: проверяемый колбэк ``_handle_delivery_result`` вызывается из
event loop, но сам никакого ожидания не делает, поэтому future заменяется
двойником.
"""

import pytest
from aiokafka.errors import (
    KafkaConnectionError,
    KafkaTimeoutError,
    MessageSizeTooLargeError,
    NotLeaderForPartitionError,
    RecordTooLargeError,
    UnsupportedForMessageFormatError,
)

from practix_analytics_collector.brokers.kafka import KafkaEventBroker, _is_record_level
from practix_analytics_collector.core.metrics import registry
from practix_analytics_collector.models.events import KafkaRecord


class _CompletedFuture:
    """Двойник future продюсера: колбэку нужны только эти два метода."""

    def __init__(self, error: BaseException | None):
        self._error = error

    def cancelled(self) -> bool:
        return False

    def exception(self) -> BaseException | None:
        return self._error


def _record(topic: str = 'ugc.clicks.v1') -> KafkaRecord:
    return KafkaRecord(topic=topic, key='anon-1', value=b'{}', headers=[])


def _healthy_broker() -> KafkaEventBroker:
    broker = KafkaEventBroker()
    broker._healthy = True
    # Продюсер не нужен: колбэк доставки его не трогает, а is_healthy
    # проверяет лишь его наличие.
    broker._producer = object()
    return broker


def _rejected_count(topic: str, error: str) -> float:
    value = registry.get_sample_value('ugc_records_rejected_total', {'topic': topic, 'error': error})
    return value or 0.0


class TestErrorClassification:
    @pytest.mark.parametrize(
        'error',
        [
            RecordTooLargeError('запись больше max_request_size'),
            MessageSizeTooLargeError('больше max.message.bytes топика'),
            UnsupportedForMessageFormatError('формат сообщения не поддержан'),
        ],
    )
    def test_record_level_errors_are_recognized(self, error):
        assert _is_record_level(error) is True

    @pytest.mark.parametrize(
        'error',
        [
            KafkaConnectionError('соединение потеряно'),
            KafkaTimeoutError('брокер не ответил'),
            NotLeaderForPartitionError('лидер сменился'),
            TimeoutError('ожидание места в буфере'),
            OSError('сеть недоступна'),
        ],
    )
    def test_cluster_level_errors_are_not_record_level(self, error):
        assert _is_record_level(error) is False


class TestDeliveryResult:
    def test_poisoned_record_does_not_degrade_the_broker(self):
        """Ключевая проверка пункта: запись отвергнута, продюсер здоров."""
        broker = _healthy_broker()
        topic = 'ugc.video_progress.v1'
        before = _rejected_count(topic, 'RecordTooLargeError')

        broker._handle_delivery_result(
            _CompletedFuture(RecordTooLargeError('запись больше max_request_size')),
            _record(topic),
        )

        assert broker.is_healthy is True, 'одна отравленная запись — не отказ кластера'
        assert _rejected_count(topic, 'RecordTooLargeError') == before + 1

    def test_connection_error_degrades_the_broker(self):
        """Обратная сторона: настоящий отказ по-прежнему переводит в degraded,
        иначе каждое следующее событие ждало бы таймаута отправки."""
        broker = _healthy_broker()

        broker._handle_delivery_result(_CompletedFuture(KafkaConnectionError('соединение потеряно')), _record())

        assert broker.is_healthy is False

    def test_successful_delivery_changes_nothing(self):
        broker = _healthy_broker()
        broker._handle_delivery_result(_CompletedFuture(None), _record())
        assert broker.is_healthy is True
