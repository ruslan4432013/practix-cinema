"""Приём событий и их попадание в Kafka.

Каждый тест проверяет не только код ответа, но и то, что сообщение реально
доехало до нужного топика с правильным ключом партиционирования. Ответ 202 сам
по себе ничего не доказывает: ручка отвечает до подтверждения от брокера.
"""

import uuid

import pytest
from helpers import (
    click_payload,
    page_view_payload,
    quality_change_payload,
    search_filter_payload,
    video_completed_payload,
    video_progress_payload,
)

from practix_analytics_collector.core.config import settings


class TestClickEvents:
    async def test_click_lands_in_clicks_topic(self, client, kafka_reader, decode_event):
        payload = click_payload()
        response = await client.post('/api/v1/events/click', json=payload)

        assert response.status_code == 202
        assert response.json()['status'] == 'accepted'
        event_id = response.json()['event_id']

        messages = await kafka_reader(settings.KAFKA_TOPIC_CLICKS, expected=1)
        matching = [m for m in messages if decode_event(m)[0]['event_id'] == event_id]
        assert matching, 'клик не найден в топике ugc.clicks.v1'

        body, key, headers = decode_event(matching[0])
        assert body['event_type'] == 'click'
        assert body['payload']['element_type'] == 'film'
        assert body['payload']['position'] == 3
        # У анонима ключом партиционирования служит anonymous_id.
        assert key == payload['anonymous_id']
        assert headers['event_type'] == 'click'
        assert headers['event_id'] == event_id
        assert headers['schema_version'] == '1'

    async def test_server_stamps_received_at(self, client, kafka_reader, decode_event):
        """received_at проставляет сервер, даже если клиент прислал своё время."""
        response = await client.post('/api/v1/events/click', json=click_payload())
        event_id = response.json()['event_id']

        messages = await kafka_reader(settings.KAFKA_TOPIC_CLICKS, expected=1)
        body = next(decode_event(m)[0] for m in messages if decode_event(m)[0]['event_id'] == event_id)
        assert body['received_at'] is not None
        assert body['schema_version'] == 1


class TestPageViewEvents:
    async def test_page_view_with_duration(self, client, kafka_reader, decode_event):
        payload = page_view_payload(duration_ms=45_000)
        response = await client.post('/api/v1/events/page-view', json=payload)
        assert response.status_code == 202
        event_id = response.json()['event_id']

        messages = await kafka_reader(settings.KAFKA_TOPIC_PAGE_VIEWS, expected=1)
        matching = [m for m in messages if decode_event(m)[0]['event_id'] == event_id]
        assert matching

        body, key, _ = decode_event(matching[0])
        assert body['payload']['page_type'] == 'film'
        assert body['payload']['duration_ms'] == 45_000
        # anonymous_id не передан — ключом становится session_id.
        assert key == payload['session_id']


class TestCustomEvents:
    async def test_quality_change(self, client, kafka_reader, decode_event):
        response = await client.post('/api/v1/events/custom', json=quality_change_payload())
        assert response.status_code == 202
        event_id = response.json()['event_id']

        messages = await kafka_reader(settings.KAFKA_TOPIC_VIDEO_EVENTS, expected=1)
        body = next(decode_event(m)[0] for m in messages if decode_event(m)[0]['event_id'] == event_id)
        assert body['event_type'] == 'video_quality_change'
        assert body['payload']['from_quality'] == '720p'
        assert body['payload']['to_quality'] == '1080p'

    async def test_video_completed_rate_is_computed_by_server(self, client, kafka_reader, decode_event):
        """Долю просмотра считает сервер, а не клиент."""
        payload = video_completed_payload(duration_ms=100_000, watched_ms=90_000)
        response = await client.post('/api/v1/events/custom', json=payload)
        event_id = response.json()['event_id']

        messages = await kafka_reader(settings.KAFKA_TOPIC_VIDEO_EVENTS, expected=1)
        body = next(decode_event(m)[0] for m in messages if decode_event(m)[0]['event_id'] == event_id)
        assert body['payload']['completion_rate'] == pytest.approx(0.9)

    async def test_video_progress_goes_to_its_own_topic(self, client, kafka_reader, decode_event):
        """Метки прогресса живут отдельно от остальных событий плеера.

        Поток тиков на два порядка массивнее: в общем топике он задавил бы
        редкие, но ценные события досмотра и смены качества.
        """
        payload = video_progress_payload(playback_position_ms=30_000, duration_ms=120_000)
        response = await client.post('/api/v1/events/custom', json=payload)
        assert response.status_code == 202
        event_id = response.json()['event_id']

        messages = await kafka_reader(settings.KAFKA_TOPIC_VIDEO_PROGRESS, expected=1)
        matching = [m for m in messages if decode_event(m)[0]['event_id'] == event_id]
        assert matching, 'метка прогресса не найдена в топике ugc.video_progress.v1'

        body, key, headers = decode_event(matching[0])
        assert body['event_type'] == 'video_progress'
        assert body['payload']['playback_position_ms'] == 30_000
        # Долю просмотра считает сервер — ETL берёт её из payload готовой.
        assert body['payload']['completion_rate'] == pytest.approx(0.25)
        assert key == payload['session_id']
        assert headers['event_type'] == 'video_progress'

    async def test_search_filters(self, client, kafka_reader, decode_event):
        response = await client.post('/api/v1/events/custom', json=search_filter_payload())
        assert response.status_code == 202
        event_id = response.json()['event_id']

        messages = await kafka_reader(settings.KAFKA_TOPIC_SEARCH_EVENTS, expected=1)
        body = next(decode_event(m)[0] for m in messages if decode_event(m)[0]['event_id'] == event_id)
        assert body['payload']['query'] == 'матрица'
        assert {f['field'] for f in body['payload']['filters']} == {'genre', 'year'}
        assert body['payload']['results_count'] == 7

    async def test_video_events_share_one_topic(self, client, kafka_reader, decode_event):
        """Смена качества и досмотр живут в одном топике — их анализируют вместе."""
        session = f'sess-{uuid.uuid4()}'
        first = await client.post('/api/v1/events/custom', json=quality_change_payload(session_id=session))
        second = await client.post('/api/v1/events/custom', json=video_completed_payload(session_id=session))
        ids = {first.json()['event_id'], second.json()['event_id']}

        messages = await kafka_reader(settings.KAFKA_TOPIC_VIDEO_EVENTS, expected=2)
        found = {decode_event(m)[0]['event_id'] for m in messages} & ids
        assert found == ids

        # Общий ключ партиционирования гарантирует их взаимный порядок.
        keys = {decode_event(m)[1] for m in messages if decode_event(m)[0]['event_id'] in ids}
        assert keys == {session}


class TestBatch:
    async def test_batch_routes_events_to_their_topics(self, client, kafka_reader, decode_event):
        session = f'sess-{uuid.uuid4()}'
        response = await client.post(
            '/api/v1/events/batch',
            json={
                'events': [
                    click_payload(session_id=session, anonymous_id=None, event_type='click'),
                    page_view_payload(session_id=session, event_type='page_view'),
                    search_filter_payload(session_id=session),
                ]
            },
        )

        assert response.status_code == 202
        body = response.json()
        assert body['accepted'] == 3
        assert body['dropped'] == 0
        assert len(body['results']) == 3

        # Каждое событие пачки ушло в свой топик.
        for topic in (
            settings.KAFKA_TOPIC_CLICKS,
            settings.KAFKA_TOPIC_PAGE_VIEWS,
            settings.KAFKA_TOPIC_SEARCH_EVENTS,
        ):
            messages = await kafka_reader(topic, expected=1)
            assert any(decode_event(m)[0]['session_id'] == session for m in messages), topic

    async def test_batch_reports_per_event_result(self, client):
        response = await client.post('/api/v1/events/batch', json={'events': [click_payload(event_type='click')]})
        results = response.json()['results']
        assert len(results) == 1
        assert results[0]['status'] == 'accepted'
        assert results[0]['event_id']


class TestDeduplication:
    async def test_same_event_id_is_published_once(self, client, kafka_reader, decode_event):
        event_id = str(uuid.uuid4())
        payload = click_payload(event_id=event_id)

        first = await client.post('/api/v1/events/click', json=payload)
        second = await client.post('/api/v1/events/click', json=payload)

        assert first.json()['status'] == 'accepted'
        assert second.json()['status'] == 'duplicate'

        messages = await kafka_reader(settings.KAFKA_TOPIC_CLICKS, expected=1)
        assert len([m for m in messages if decode_event(m)[0]['event_id'] == event_id]) == 1

    async def test_client_supplied_event_id_is_echoed(self, client):
        event_id = str(uuid.uuid4())
        response = await client.post('/api/v1/events/click', json=click_payload(event_id=event_id))
        assert response.json()['event_id'] == event_id
