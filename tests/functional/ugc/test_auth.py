"""Атрибуция событий пользователю и невозможность её подделать.

Ключевое свойство, которое здесь проверяется: ``user_id`` берётся только из
подписи токена. Всё остальное — следствия принятого решения о том, что
аутентификация опциональна и её проблемы не приводят к отказу.
"""

import uuid

from helpers import auth_header, click_payload, make_access_token

from core.config import settings


class TestAuthenticatedEvents:
    async def test_user_id_comes_from_token(self, client, kafka_reader, decode_event):
        user_id = str(uuid.uuid4())
        response = await client.post('/api/v1/events/click', json=click_payload(), headers=auth_header(user_id))
        assert response.status_code == 202
        event_id = response.json()['event_id']

        messages = await kafka_reader(settings.KAFKA_TOPIC_CLICKS, expected=1)
        body, key, _ = next(decode_event(m) for m in messages if decode_event(m)[0]['event_id'] == event_id)
        assert body['user_id'] == user_id
        assert body['is_authenticated'] is True
        # У аутентифицированного пользователя ключ партиционирования — user_id,
        # даже если в запросе передан anonymous_id.
        assert key == user_id

    async def test_anonymous_event_has_no_user(self, client, kafka_reader, decode_event):
        response = await client.post('/api/v1/events/click', json=click_payload())
        event_id = response.json()['event_id']

        messages = await kafka_reader(settings.KAFKA_TOPIC_CLICKS, expected=1)
        body = next(decode_event(m)[0] for m in messages if decode_event(m)[0]['event_id'] == event_id)
        assert body['user_id'] is None
        assert body['is_authenticated'] is False


class TestTokenSpoofing:
    async def test_user_id_in_body_is_rejected(self, client):
        """Поля user_id во входной модели нет — extra='forbid' даёт 422."""
        response = await client.post('/api/v1/events/click', json=click_payload(user_id=str(uuid.uuid4())))
        assert response.status_code == 422
        assert any(error['type'] == 'extra_forbidden' for error in response.json()['detail'])

    async def test_is_authenticated_in_body_is_rejected(self, client):
        response = await client.post('/api/v1/events/click', json=click_payload(is_authenticated=True))
        assert response.status_code == 422

    async def test_received_at_in_body_is_rejected(self, client):
        response = await client.post('/api/v1/events/click', json=click_payload(received_at='2020-01-01T00:00:00Z'))
        assert response.status_code == 422

    async def test_token_signed_with_another_secret_is_ignored(self, client, kafka_reader, decode_event):
        """Чужая подпись не даёт атрибутировать событие — оно становится анонимным."""
        import datetime

        import jwt

        now = datetime.datetime.now(datetime.UTC)
        forged = jwt.encode(
            {
                'sub': str(uuid.uuid4()),
                'iat': now,
                'nbf': now,
                'exp': now + datetime.timedelta(hours=1),
                'jti': str(uuid.uuid4()),
                'type': 'access',
                'fresh': False,
                'roles': ['admin'],
            },
            'definitely-not-our-secret',
            algorithm='HS256',
        )

        response = await client.post(
            '/api/v1/events/click',
            json=click_payload(),
            headers={'Authorization': f'Bearer {forged}'},
        )
        assert response.status_code == 202
        event_id = response.json()['event_id']

        messages = await kafka_reader(settings.KAFKA_TOPIC_CLICKS, expected=1)
        body = next(decode_event(m)[0] for m in messages if decode_event(m)[0]['event_id'] == event_id)
        assert body['user_id'] is None
        assert body['is_authenticated'] is False


class TestGracefulAuthDegradation:
    """Проблема с токеном не должна стоить события."""

    async def test_expired_token_is_accepted_as_anonymous(self, client):
        response = await client.post(
            '/api/v1/events/click',
            json=click_payload(),
            headers={'Authorization': f'Bearer {make_access_token(expired=True)}'},
        )
        assert response.status_code == 202
        assert response.json()['status'] == 'accepted'

    async def test_malformed_token_is_accepted_as_anonymous(self, client):
        response = await client.post(
            '/api/v1/events/click',
            json=click_payload(),
            headers={'Authorization': 'Bearer not.a.real.token'},
        )
        assert response.status_code == 202

    async def test_non_uuid_subject_is_accepted_as_anonymous(self, client, kafka_reader, decode_event):
        """Токен от чужого эмитента: sub есть, но это не UUID."""
        response = await client.post(
            '/api/v1/events/click',
            json=click_payload(),
            headers={'Authorization': f'Bearer {make_access_token(user_id="not-a-uuid")}'},
        )
        assert response.status_code == 202
        event_id = response.json()['event_id']

        messages = await kafka_reader(settings.KAFKA_TOPIC_CLICKS, expected=1)
        body = next(decode_event(m)[0] for m in messages if decode_event(m)[0]['event_id'] == event_id)
        assert body['user_id'] is None
