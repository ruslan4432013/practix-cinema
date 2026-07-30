"""Деградация: приём событий при недоступной инфраструктуре.

Это главное требование спринта — сайт не должен ломаться из-за аналитики.
Тесты используют брокер-заглушку (``stub_client``), чтобы «ронять» транспорт по
команде, не останавливая настоящий кластер: иначе прогон стал бы медленным, а
тесты — зависимыми друг от друга.
"""

import json

from helpers import click_payload, page_view_payload

from practix_analytics_collector.brokers.base import RecordRejectedError
from practix_analytics_collector.core.config import settings
from practix_analytics_collector.models.events import KafkaRecord


class TestKafkaUnavailable:
    async def test_event_is_buffered_not_rejected(self, stub_client, stub_broker, redis_client):
        stub_broker.available = False

        response = await stub_client.post('/api/v1/events/click', json=click_payload())

        # Ключевое утверждение: клиент НЕ получает ошибку.
        assert response.status_code == 202
        assert response.json()['status'] == 'buffered'
        assert await redis_client.llen(settings.UGC_FALLBACK_KEY) == 1

    async def test_buffered_record_keeps_topic_and_key(self, stub_client, stub_broker, redis_client):
        stub_broker.available = False
        payload = click_payload()

        await stub_client.post('/api/v1/events/click', json=payload)

        raw = await redis_client.lindex(settings.UGC_FALLBACK_KEY, 0)
        record = KafkaRecord.from_dict(json.loads(raw))
        assert record.topic == settings.KAFKA_TOPIC_CLICKS
        assert record.key == payload['anonymous_id']
        # Тело сохранено целиком и готово к отправке байт-в-байт.
        assert json.loads(record.value)['event_type'] == 'click'

    async def test_batch_is_buffered_entirely(self, stub_client, stub_broker, redis_client):
        stub_broker.available = False

        response = await stub_client.post(
            '/api/v1/events/batch',
            json={'events': [page_view_payload(event_type='page_view') for _ in range(5)]},
        )

        assert response.status_code == 202
        body = response.json()
        assert body['buffered'] == 5
        assert body['dropped'] == 0
        assert await redis_client.llen(settings.UGC_FALLBACK_KEY) == 5


class TestDrain:
    async def test_buffered_events_are_delivered_after_recovery(self, stub_client, stub_broker, redis_client):
        from practix_analytics_collector.services.providers import get_fallback_buffer

        stub_broker.available = False
        for _ in range(3):
            await stub_client.post('/api/v1/events/click', json=click_payload())
        assert await redis_client.llen(settings.UGC_FALLBACK_KEY) == 3

        stub_broker.available = True
        delivered = await get_fallback_buffer().drain_once()

        assert delivered == 3
        assert await redis_client.llen(settings.UGC_FALLBACK_KEY) == 0
        assert len(stub_broker.records) == 3
        assert {r.topic for r in stub_broker.records} == {settings.KAFKA_TOPIC_CLICKS}

    async def test_drain_does_nothing_while_broker_is_down(self, stub_client, stub_broker, redis_client):
        from practix_analytics_collector.services.providers import get_fallback_buffer

        stub_broker.available = False
        await stub_client.post('/api/v1/events/click', json=click_payload())

        delivered = await get_fallback_buffer().drain_once()

        assert delivered == 0
        # Событие осталось в буфере, а не потерялось.
        assert await redis_client.llen(settings.UGC_FALLBACK_KEY) == 1

    async def test_record_survives_broker_failing_mid_drain(self, stub_client, stub_broker, redis_client):
        """Брокер отваливается в процессе дренажа — остаток обязан уцелеть."""
        from practix_analytics_collector.services.providers import get_fallback_buffer

        stub_broker.available = False
        for _ in range(4):
            await stub_client.post('/api/v1/events/click', json=click_payload())

        # Транспорт «оживает» ровно на две записи, затем снова падает.
        original_publish = stub_broker.publish_now
        calls = {'count': 0}

        async def flaky_publish(record):
            calls['count'] += 1
            if calls['count'] > 2:
                stub_broker.available = False
            await original_publish(record)

        stub_broker.available = True
        stub_broker.publish_now = flaky_publish

        delivered = await get_fallback_buffer().drain_once()

        assert delivered == 2
        # Ни одна запись не потеряна: 4 = 2 доставленных + 2 оставшихся в очереди.
        remaining = await redis_client.llen(settings.UGC_FALLBACK_KEY)
        in_flight = await redis_client.llen('ugc:fallback:processing')
        assert delivered + remaining + in_flight == 4

    async def test_in_flight_records_are_recovered(self, stub_broker, redis_client):
        """Записи, зависшие в in-flight после аварии, возвращаются в очередь.

        Имитируем падение процесса между извлечением записи и её публикацией:
        именно этот сценарий терял бы данные при наивном RPOP.
        """
        from practix_analytics_collector.services.fallback_buffer import FallbackBuffer

        record = KafkaRecord(topic=settings.KAFKA_TOPIC_CLICKS, key='k', value=b'{"event_type":"click"}')
        await redis_client.lpush('ugc:fallback:processing', json.dumps(record.to_dict()))

        buffer = FallbackBuffer(lambda: redis_client, stub_broker)
        stub_broker.available = True
        delivered = await buffer.drain_once()

        assert delivered == 1
        assert await redis_client.llen('ugc:fallback:processing') == 0
        assert len(stub_broker.records) == 1


class TestPoisonedRecord:
    """Отказ КОНКРЕТНОЙ записи — не отказ кластера.

    Раньше любая неудача доставки помечала продюсер нездоровым, и одно
    «отравленное» сообщение (например, больше `max.message.bytes`) уводило весь
    поток событий в Redis при полностью работающей Kafka. Хуже того, уход
    записи в DLQ требует живого брокера — то есть запись, сама же «сломавшая»
    брокер, не могла из буфера уехать никогда.
    """

    async def test_rejected_record_is_buffered_and_broker_stays_healthy(self, stub_client, stub_broker, redis_client):
        stub_broker.reject_records = True

        response = await stub_client.post('/api/v1/events/click', json=click_payload())

        assert response.status_code == 202
        assert response.json()['status'] == 'buffered'
        assert stub_broker.is_healthy is True, 'отказ записи не должен объявлять кластер лежащим'
        assert await redis_client.llen(settings.UGC_FALLBACK_KEY) == 1

    async def test_rejected_record_goes_to_dlq_on_the_first_drain(self, stub_client, stub_broker, redis_client):
        """Повторять такую запись бессмысленно: попытки не тратятся."""
        from practix_analytics_collector.services.providers import get_fallback_buffer

        stub_broker.reject_records = True
        await stub_client.post('/api/v1/events/click', json=click_payload())

        await get_fallback_buffer().drain_once()

        assert await redis_client.llen(settings.UGC_FALLBACK_KEY) == 0
        assert await redis_client.llen('ugc:fallback:processing') == 0
        assert [record.topic for record in stub_broker.records] == [settings.KAFKA_TOPIC_DLQ]

    async def test_good_records_keep_flowing_past_a_poisoned_one(self, stub_client, stub_broker, redis_client):
        """Отравленная запись не задерживает очередь: остальные доезжают."""
        from practix_analytics_collector.services.providers import get_fallback_buffer

        stub_broker.available = False
        for _ in range(3):
            await stub_client.post('/api/v1/events/click', json=click_payload())

        # Брокер вернулся, но самую старую запись он принимать отказывается.
        stub_broker.available = True
        original_publish_now = stub_broker.publish_now
        first = {'seen': False}

        async def reject_first(record):
            if not first['seen'] and record.topic != settings.KAFKA_TOPIC_DLQ:
                first['seen'] = True
                raise RecordRejectedError('poisoned record')
            await original_publish_now(record)

        stub_broker.publish_now = reject_first

        await get_fallback_buffer().drain_once()

        assert await redis_client.llen(settings.UGC_FALLBACK_KEY) == 0
        topics = [record.topic for record in stub_broker.records]
        assert topics.count(settings.KAFKA_TOPIC_CLICKS) == 2
        assert topics.count(settings.KAFKA_TOPIC_DLQ) == 1


class TestTotalFailure:
    async def test_event_is_dropped_but_never_returns_5xx(self, stub_client, stub_broker, monkeypatch):
        """Недоступны и Kafka, и Redis — событие теряется, но клиент видит 202."""
        from practix_analytics_collector.services import providers

        stub_broker.available = False

        async def failing_push(_record):
            return False

        monkeypatch.setattr(providers.fallback_buffer, 'push', failing_push)

        response = await stub_client.post('/api/v1/events/click', json=click_payload())

        assert response.status_code == 202
        assert response.json()['status'] == 'dropped'


class BrokenRedis:
    """Клиент Redis, падающий на любой операции."""

    def __getattr__(self, _name):
        async def _fail(*_args, **_kwargs):
            raise ConnectionError('redis is down')

        return _fail


class TestRedisUnavailable:
    """Redis обслуживает дедупликацию, лимиты и буфер — но ни одно из этого
    не является обязательным для приёма события."""

    async def test_event_is_accepted_when_redis_is_down(self, stub_broker, redis_client):
        """Kafka жива, Redis лежит: событие обязано доехать до брокера."""
        from httpx import ASGITransport, AsyncClient

        import practix_analytics_collector.main as main
        from practix_analytics_collector.db import redis as redis_db
        from practix_analytics_collector.services import providers
        from practix_analytics_collector.services.event_service import EventService
        from practix_analytics_collector.services.fallback_buffer import FallbackBuffer

        broken = BrokenRedis()
        buffer = FallbackBuffer(lambda: broken, stub_broker)
        providers.broker = stub_broker
        providers.fallback_buffer = buffer
        providers.event_service = EventService(stub_broker, buffer, lambda: broken)
        redis_db.redis = broken

        try:
            transport = ASGITransport(app=main.app)
            async with AsyncClient(transport=transport, base_url='http://test') as ac:
                response = await ac.post('/api/v1/events/click', json=click_payload())
        finally:
            redis_db.redis = redis_client

        # Дедупликация и rate limit молча отключились (fail-open),
        # событие ушло в Kafka.
        assert response.status_code == 202
        assert response.json()['status'] == 'accepted'
        assert len(stub_broker.records) == 1

    async def test_dedup_is_skipped_when_redis_is_down(self, stub_broker):
        """Без Redis дубликаты не гасятся — это осознанный размен.

        Гарантия «хотя бы один раз» важнее подавления дублей: пропущенный дубль
        поправим на стороне аналитики, потерянное событие — нет.
        """
        from practix_analytics_collector.services.event_service import EventService
        from practix_analytics_collector.services.fallback_buffer import FallbackBuffer

        broken = BrokenRedis()
        service = EventService(stub_broker, FallbackBuffer(lambda: broken, stub_broker), lambda: broken)

        assert await service._is_duplicate('any-event-id') is False
