"""Фикстуры функциональных тестов сервиса сбора пользовательских действий.

Приложение запускается **в процессе** через httpx ``ASGITransport`` — тот же
приём, что в ``tests/functional/auth/conftest.py``. Kafka и Redis при этом
настоящие: подменять их моками бессмысленно, потому что проверяется ровно то
поведение, которое возникает на границе с ними (партиционирование, заголовки
сообщений, буферизация при отказе брокера).
"""

import asyncio
import json
import uuid

import pytest
from aiokafka import AIOKafkaConsumer
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis

import practix_analytics_collector.main as main
from practix_analytics_collector.brokers.base import BrokerUnavailableError, EventBroker, RecordRejectedError
from practix_analytics_collector.brokers.kafka import KafkaEventBroker
from practix_analytics_collector.brokers.topics import all_topics
from practix_analytics_collector.core.config import settings
from practix_analytics_collector.db import redis as redis_db
from practix_analytics_collector.services import providers
from practix_analytics_collector.services.event_service import EventService
from practix_analytics_collector.services.fallback_buffer import FallbackBuffer

# Секрет должен совпадать с тем, которым тесты подписывают токены.
JWT_SECRET = settings.AUTHJWT_SECRET_KEY


@pytest.fixture
async def redis_client():
    """Клиент Redis, очищающий тестовую базу до и после теста.

    Адрес берётся из свойств настроек, а не из ``REDIS_HOST`` напрямую: у
    коллектора собственный экземпляр Redis (compose: ``redis-ugc``), и тест
    обязан чистить именно его.

    ``flushdb``, а НЕ ``flushall``: даже в отдельном экземпляре привычка к
    ``flushall`` рано или поздно уносит чужие данные.

    Пересоздаётся на каждый тест: pytest-asyncio даёт каждому тесту свой event
    loop, а соединение, созданное в чужом цикле, к нему не привязать.
    """
    client = Redis(
        host=settings.ugc_redis_host,
        port=settings.ugc_redis_port,
        db=settings.UGC_REDIS_DB,
        decode_responses=True,
    )
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


class StubBroker(EventBroker):
    """Брокер-заглушка, которым управляет тест.

    Нужен, чтобы проверить деградацию, не останавливая настоящий кластер:
    остановка брокера в середине прогона сделала бы тесты медленными и
    зависимыми друг от друга.
    """

    def __init__(self):
        self.records = []
        self.available = True
        # Брокер жив, но отвергает записи из-за их собственных свойств
        # (аналог RecordTooLargeError). DLQ-топик при этом принимается: иначе
        # проверить путь «отравленная запись → DLQ» было бы нечем.
        self.reject_records = False

    async def publish(self, record):
        if not self.available:
            raise BrokerUnavailableError('stub broker is down')
        if self.reject_records and record.topic != settings.KAFKA_TOPIC_DLQ:
            raise RecordRejectedError('stub broker rejected the record itself')
        self.records.append(record)

    async def publish_now(self, record):
        await self.publish(record)

    @property
    def is_healthy(self) -> bool:
        return self.available


async def _build_client(broker: EventBroker, redis_client: Redis):
    """Собирает приложение поверх переданного брокера."""
    redis_db.redis = redis_client
    buffer = FallbackBuffer(redis_db.get_redis, broker)
    if isinstance(broker, KafkaEventBroker):
        broker.set_delivery_failure_handler(buffer.push)

    providers.broker = broker
    providers.fallback_buffer = buffer
    providers.event_service = EventService(broker, buffer, redis_db.get_redis)
    return buffer


@pytest.fixture
async def kafka_broker():
    """Настоящий продюсер Kafka."""
    broker = KafkaEventBroker()
    await broker.start()
    assert broker.is_healthy, 'Kafka must be reachable for functional tests'
    yield broker
    await broker.stop()


@pytest.fixture
async def client(kafka_broker, redis_client):
    """HTTP-клиент приложения, работающего против настоящей Kafka."""
    buffer = await _build_client(kafka_broker, redis_client)
    transport = ASGITransport(app=main.app)
    try:
        async with AsyncClient(transport=transport, base_url='http://test') as ac:
            yield ac
    finally:
        await buffer.stop()
        providers.broker = None
        providers.fallback_buffer = None
        providers.event_service = None
        redis_db.redis = None


@pytest.fixture
async def stub_broker():
    return StubBroker()


@pytest.fixture
async def stub_client(stub_broker, redis_client):
    """HTTP-клиент приложения поверх управляемого брокера-заглушки."""
    buffer = await _build_client(stub_broker, redis_client)
    transport = ASGITransport(app=main.app)
    try:
        async with AsyncClient(transport=transport, base_url='http://test') as ac:
            yield ac
    finally:
        await buffer.stop()
        providers.broker = None
        providers.fallback_buffer = None
        providers.event_service = None
        redis_db.redis = None


@pytest.fixture
async def kafka_reader():
    """Читает сообщения из топиков UGC.

    Каждый тест получает свою consumer-группу и читает с конца: топики между
    тестами не пересоздаются, и без этого тест видел бы события соседних тестов.
    """
    consumers = []

    async def read(topic: str, expected: int = 1, timeout: float = 15.0) -> list:
        consumer = AIOKafkaConsumer(
            topic,
            bootstrap_servers=settings.kafka_bootstrap_list,
            group_id=f'test-{uuid.uuid4()}',
            auto_offset_reset='earliest',
            enable_auto_commit=False,
        )
        await consumer.start()
        consumers.append(consumer)

        messages = []
        deadline = asyncio.get_running_loop().time() + timeout
        while len(messages) < expected and asyncio.get_running_loop().time() < deadline:
            batches = await consumer.getmany(timeout_ms=1000)
            for records in batches.values():
                messages.extend(records)
        return messages

    yield read

    for consumer in consumers:
        await consumer.stop()


@pytest.fixture
def decode_event():
    """Разбирает сообщение Kafka в (тело, ключ, заголовки)."""

    def _decode(message):
        headers = {name: value.decode('utf-8') for name, value in (message.headers or [])}
        return (
            json.loads(message.value),
            message.key.decode('utf-8') if message.key else None,
            headers,
        )

    return _decode


@pytest.fixture
def topics():
    return all_topics()
