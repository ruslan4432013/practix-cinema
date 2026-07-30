"""Валидация входных данных и ограничения размера.

Ингест-ручка публичная и анонимная, поэтому проверяется не только то, что
корректные данные принимаются, но и то, что некорректные и злонамеренные
отклоняются до всякой работы.
"""

from helpers import click_payload, page_view_payload, search_filter_payload

from core.config import settings


class TestSchemaStrictness:
    async def test_unknown_field_is_rejected(self, client):
        """extra='forbid': молча проглоченное поле — это потерянные данные."""
        response = await client.post('/api/v1/events/click', json=click_payload(unexpected='value'))
        assert response.status_code == 422

    async def test_missing_required_field_is_rejected(self, client):
        payload = click_payload()
        del payload['session_id']
        response = await client.post('/api/v1/events/click', json=payload)
        assert response.status_code == 422

    async def test_unknown_enum_value_is_rejected(self, client):
        response = await client.post('/api/v1/events/click', json=click_payload(element_type='../../etc/passwd'))
        assert response.status_code == 422

    async def test_unknown_event_type_in_custom_is_rejected(self, client):
        response = await client.post(
            '/api/v1/events/custom',
            json={'event_type': 'definitely_not_a_real_event', 'session_id': 's'},
        )
        assert response.status_code == 422

    async def test_click_endpoint_rejects_foreign_event_type(self, client):
        """На /click нельзя прислать событие другого типа."""
        response = await client.post('/api/v1/events/click', json=click_payload(event_type='page_view'))
        assert response.status_code == 422


class TestUrlValidation:
    async def test_javascript_scheme_is_rejected(self, client):
        """Значение попадёт в дашборд аналитика — санитизируем на входе."""
        response = await client.post('/api/v1/events/page-view', json=page_view_payload(page_url='javascript:alert(1)'))
        assert response.status_code == 422

    async def test_data_scheme_is_rejected(self, client):
        response = await client.post(
            '/api/v1/events/page-view', json=page_view_payload(page_url='data:text/html,<script>')
        )
        assert response.status_code == 422

    async def test_overlong_url_is_rejected(self, client):
        response = await client.post(
            '/api/v1/events/page-view', json=page_view_payload(page_url='http://localhost/' + 'x' * 3000)
        )
        assert response.status_code == 422


class TestPropertiesLimits:
    async def test_nested_properties_are_rejected(self, client):
        """Вложенность запрещена — защита от рекурсивной полезной нагрузки."""
        response = await client.post('/api/v1/events/click', json=click_payload(properties={'a': {'b': {'c': 1}}}))
        assert response.status_code == 422

    async def test_too_many_property_keys_are_rejected(self, client):
        response = await client.post(
            '/api/v1/events/click', json=click_payload(properties={f'k{i}': i for i in range(50)})
        )
        assert response.status_code == 422

    async def test_overlong_property_value_is_rejected(self, client):
        response = await client.post('/api/v1/events/click', json=click_payload(properties={'note': 'x' * 1000}))
        assert response.status_code == 422

    async def test_flat_scalar_properties_are_accepted(self, client):
        response = await client.post(
            '/api/v1/events/click',
            json=click_payload(properties={'ab_group': 'B', 'rank': 3, 'score': 0.5, 'is_new': True}),
        )
        assert response.status_code == 202


class TestBatchLimits:
    async def test_batch_over_limit_is_rejected(self, client):
        events = [page_view_payload(event_type='page_view') for _ in range(settings.UGC_MAX_BATCH_SIZE + 10)]
        response = await client.post('/api/v1/events/batch', json={'events': events})
        assert response.status_code == 422

    async def test_empty_batch_is_rejected(self, client):
        response = await client.post('/api/v1/events/batch', json={'events': []})
        assert response.status_code == 422

    async def test_batch_at_limit_is_accepted(self, client):
        events = [page_view_payload(event_type='page_view') for _ in range(settings.UGC_MAX_BATCH_SIZE)]
        response = await client.post('/api/v1/events/batch', json={'events': events})
        assert response.status_code == 202
        assert response.json()['accepted'] == settings.UGC_MAX_BATCH_SIZE


class TestBodySizeLimit:
    async def test_oversized_body_is_rejected_with_413(self, client):
        """Большое тело отсекается до разбора Pydantic."""
        payload = click_payload(element_id='x' * (settings.UGC_MAX_BODY_BYTES + 1000))
        response = await client.post('/api/v1/events/click', json=payload)
        assert response.status_code == 413

    async def test_body_within_limit_is_processed(self, client):
        response = await client.post('/api/v1/events/click', json=click_payload())
        assert response.status_code == 202


class TestTimestampSanity:
    async def test_far_future_timestamp_is_rejected(self, client):
        """Сбитые или подделанные часы клиента испортили бы временные ряды."""
        response = await client.post('/api/v1/events/click', json=click_payload(event_timestamp='2999-01-01T00:00:00Z'))
        assert response.status_code == 422

    async def test_ancient_timestamp_is_rejected(self, client):
        response = await client.post('/api/v1/events/click', json=click_payload(event_timestamp='1990-01-01T00:00:00Z'))
        assert response.status_code == 422


class TestDomainInvariants:
    async def test_watched_much_greater_than_duration_is_rejected(self, client):
        response = await client.post(
            '/api/v1/events/custom',
            json={
                'event_type': 'video_completed',
                'session_id': 's',
                'film_id': '3d825f60-9fff-4dfe-b294-1a45fa1e115d',
                'duration_ms': 1000,
                'watched_ms': 900_000,
            },
        )
        assert response.status_code == 422

    async def test_progress_position_beyond_duration_is_rejected(self, client):
        """Позиция за пределами длительности испортила бы кривую досмотра."""
        response = await client.post(
            '/api/v1/events/custom',
            json={
                'event_type': 'video_progress',
                'session_id': 's',
                'film_id': '3d825f60-9fff-4dfe-b294-1a45fa1e115d',
                'playback_position_ms': 200_000,
                'duration_ms': 100_000,
            },
        )
        assert response.status_code == 422

    async def test_progress_position_within_tolerance_is_accepted(self, client):
        """5 % допуска — на погрешность плеера, а не на мусор."""
        response = await client.post(
            '/api/v1/events/custom',
            json={
                'event_type': 'video_progress',
                'session_id': 's',
                'film_id': '3d825f60-9fff-4dfe-b294-1a45fa1e115d',
                'playback_position_ms': 102_000,
                'duration_ms': 100_000,
            },
        )
        assert response.status_code == 202

    async def test_too_many_search_filters_are_rejected(self, client):
        payload = search_filter_payload(filters=[{'field': 'genre', 'value': f'g{i}'} for i in range(30)])
        response = await client.post('/api/v1/events/custom', json=payload)
        assert response.status_code == 422

    async def test_negative_duration_is_rejected(self, client):
        response = await client.post('/api/v1/events/page-view', json=page_view_payload(duration_ms=-1))
        assert response.status_code == 422
