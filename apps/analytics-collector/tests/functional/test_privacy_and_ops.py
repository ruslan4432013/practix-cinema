"""Приватность, ограничение частоты и служебные ручки."""

import uuid

from helpers import click_payload

from practix_analytics_collector.core.config import settings
from practix_analytics_collector.core.privacy import hash_ip


class TestPrivacy:
    async def test_raw_ip_never_reaches_kafka(self, client, kafka_reader, decode_event):
        """В сообщение уходит только хеш: сырой адрес не покидает процесс.

        Заголовок собран как его собирает Nginx ($proxy_add_x_forwarded_for):
        слева — то, что прислал клиент, справа — дописанный прокси настоящий
        адрес. Клиентом считается именно правый.
        """
        client_ip = '203.0.113.77'
        response = await client.post(
            '/api/v1/events/click',
            json=click_payload(),
            headers={'X-Forwarded-For': f'10.0.0.1, {client_ip}'},
        )
        event_id = response.json()['event_id']

        messages = await kafka_reader(settings.KAFKA_TOPIC_CLICKS, expected=1)
        message = next(m for m in messages if decode_event(m)[0]['event_id'] == event_id)
        body, _, _ = decode_event(message)

        assert client_ip not in message.value.decode('utf-8')
        assert body['context']['ip_hash'] == hash_ip(client_ip)
        assert 'ip' not in body['context']

    async def test_ip_hash_is_stable_and_salted(self):
        assert hash_ip('203.0.113.77') == hash_ip('203.0.113.77')
        assert hash_ip('203.0.113.77') != hash_ip('203.0.113.78')
        # Хеш — не сам адрес и не его тривиальное преобразование.
        assert '203.0.113.77' not in hash_ip('203.0.113.77')

    async def test_user_agent_is_truncated_and_parsed(self, client, kafka_reader, decode_event):
        long_ua = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) ' + 'x' * 2000
        response = await client.post('/api/v1/events/click', json=click_payload(), headers={'User-Agent': long_ua})
        event_id = response.json()['event_id']

        messages = await kafka_reader(settings.KAFKA_TOPIC_CLICKS, expected=1)
        body = next(decode_event(m)[0] for m in messages if decode_event(m)[0]['event_id'] == event_id)

        assert len(body['context']['user_agent']) <= settings.UGC_MAX_USER_AGENT_LENGTH
        assert body['context']['device_type'] == 'mobile'


class TestRateLimit:
    async def test_requests_over_limit_get_429(self, client, redis_client, monkeypatch):
        monkeypatch.setattr(settings, 'UGC_RATE_LIMIT_ENABLED', True)
        monkeypatch.setattr(settings, 'UGC_RATE_LIMIT_TIMES', 5)

        statuses = []
        for _ in range(8):
            response = await client.post('/api/v1/events/click', json=click_payload())
            statuses.append(response.status_code)

        assert 429 in statuses
        assert statuses.count(202) <= 5

    async def test_forged_forwarded_for_does_not_reset_the_counter(self, client, redis_client, monkeypatch):
        """Подделка X-Forwarded-For не даёт нового счётчика лимита.

        Nginx дописывает реальный адрес справа, поэтому что бы клиент ни
        подставил слева, идентичность у всех запросов одна. Если бы сервис
        доверял левому элементу, лимит по IP переставал бы существовать: на
        каждый запрос — свой счётчик.
        """
        monkeypatch.setattr(settings, 'UGC_RATE_LIMIT_ENABLED', True)
        monkeypatch.setattr(settings, 'UGC_RATE_LIMIT_TIMES', 5)
        real_ip = f'203.0.113.{uuid.uuid4().int % 200 + 10}'

        statuses = []
        for attempt in range(8):
            response = await client.post(
                '/api/v1/events/click',
                json=click_payload(),
                headers={'X-Forwarded-For': f'10.10.10.{attempt}, {real_ip}'},
            )
            statuses.append(response.status_code)

        assert 429 in statuses
        assert statuses.count(202) <= 5

    async def test_identity_limit_survives_ip_rotation(self, client, monkeypatch):
        """Лимит по сессии независим от IP и списывается в том же pipeline.

        Смена адреса на каждый запрос обходит только лимит по IP; сессия
        остаётся той же, и её счётчик доводит клиента до 429.
        """
        monkeypatch.setattr(settings, 'UGC_RATE_LIMIT_ENABLED', True)
        # Заведомо не мешает: проверяем именно лимит по идентичности.
        monkeypatch.setattr(settings, 'UGC_RATE_LIMIT_TIMES', 10_000)
        monkeypatch.setattr(settings, 'UGC_IDENTITY_RATE_LIMIT_TIMES', 5)
        session = f'sess-{uuid.uuid4()}'

        statuses = []
        for attempt in range(8):
            response = await client.post(
                '/api/v1/events/click',
                json=click_payload(session_id=session),
                headers={'X-Forwarded-For': f'203.0.113.{attempt + 1}'},
            )
            statuses.append(response.status_code)

        assert 429 in statuses
        assert statuses.count(202) <= 5

    async def test_429_carries_retry_after(self, client, monkeypatch):
        monkeypatch.setattr(settings, 'UGC_RATE_LIMIT_ENABLED', True)
        monkeypatch.setattr(settings, 'UGC_RATE_LIMIT_TIMES', 1)

        await client.post('/api/v1/events/click', json=click_payload())
        for _ in range(4):
            response = await client.post('/api/v1/events/click', json=click_payload())
            if response.status_code == 429:
                assert 'retry-after' in {k.lower() for k in response.headers}
                return
        raise AssertionError('лимит не сработал')

    async def test_batch_consumes_limit_proportionally(self, client, redis_client, monkeypatch):
        """Пачка не должна быть способом обойти лимит."""
        monkeypatch.setattr(settings, 'UGC_RATE_LIMIT_ENABLED', True)
        monkeypatch.setattr(settings, 'UGC_RATE_LIMIT_TIMES', 100)

        from helpers import page_view_payload

        response = await client.post(
            '/api/v1/events/batch',
            json={'events': [page_view_payload(event_type='page_view') for _ in range(20)]},
        )
        assert response.status_code == 202

        # Пачка из 20 событий израсходовала 20 единиц лимита, а не одну.
        keys = await redis_client.keys('ugc:ratelimit:*')
        assert keys
        counters = []
        for key in keys:
            counters.append(int(await redis_client.get(key)))
        assert max(counters) >= 20

    async def test_health_endpoints_are_exempt(self, client, monkeypatch):
        monkeypatch.setattr(settings, 'UGC_RATE_LIMIT_ENABLED', True)
        monkeypatch.setattr(settings, 'UGC_RATE_LIMIT_TIMES', 1)

        for _ in range(10):
            assert (await client.get('/health/live')).status_code == 200


class TestHealthAndMetrics:
    async def test_liveness_is_independent_of_broker(self, stub_client, stub_broker):
        """Перезапуск процесса не чинит упавшую Kafka — проба живости
        не должна на неё смотреть."""
        stub_broker.available = False
        response = await stub_client.get('/health/live')
        assert response.status_code == 200
        assert response.json()['status'] == 'ok'

    async def test_readiness_reflects_broker_state(self, stub_client, stub_broker):
        assert (await stub_client.get('/health/ready')).json()['broker_connected'] is True

        stub_broker.available = False
        body = (await stub_client.get('/health/ready')).json()
        assert body['broker_connected'] is False
        assert body['status'] == 'degraded'

    async def test_readiness_stays_200_while_buffer_works(self, stub_client, stub_broker):
        """Деградация — не повод выводить инстанс из балансировки:
        события принимаются без потерь."""
        stub_broker.available = False
        response = await stub_client.get('/health/ready')
        assert response.status_code == 200

    async def test_readiness_reports_buffer_size(self, stub_client, stub_broker):
        stub_broker.available = False
        await stub_client.post('/api/v1/events/click', json=click_payload())

        body = (await stub_client.get('/health/ready')).json()
        assert body['buffered_events'] == 1

    async def test_metrics_expose_event_counters(self, client):
        await client.post('/api/v1/events/click', json=click_payload())

        response = await client.get('/metrics')
        assert response.status_code == 200
        text = response.text
        assert 'ugc_events_received_total' in text
        assert 'ugc_events_published_total' in text
        assert 'ugc_events_dropped_total' in text or 'ugc_events_dropped' in text

    async def test_openapi_is_served_on_its_own_path(self, client):
        """Путь отличается от /api/openapi: тот за Nginx занят Movies API."""
        response = await client.get('/api/analytics/openapi.json')
        assert response.status_code == 200
        assert '/api/v1/events/click' in response.json()['paths']


class TestRequestId:
    async def test_request_id_is_echoed_and_forwarded_to_kafka(self, client, kafka_reader, decode_event):
        request_id = f'test-{uuid.uuid4()}'
        response = await client.post('/api/v1/events/click', json=click_payload(), headers={'X-Request-Id': request_id})
        assert response.headers['X-Request-Id'] == request_id
        event_id = response.json()['event_id']

        messages = await kafka_reader(settings.KAFKA_TOPIC_CLICKS, expected=1)
        _, _, headers = next(decode_event(m) for m in messages if decode_event(m)[0]['event_id'] == event_id)
        assert headers['x-request-id'] == request_id

    async def test_missing_request_id_is_generated_not_rejected(self, client):
        """Отличие от rest/ и auth/: sendBeacon не умеет ставить заголовки,
        поэтому требование заголовка сломало бы сбор beacon-событий."""
        response = await client.post('/api/v1/events/click', json=click_payload())
        assert response.status_code == 202
        assert response.headers.get('X-Request-Id')
