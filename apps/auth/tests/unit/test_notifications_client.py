"""Продюсер отчётного события: чем бы он ни кончился, регистрация не страдает.

Это главное свойство модуля и единственное, ради которого стоит тест: провал
доставки события означает «человек не получил приветственное письмо», а не
«регистрация не удалась». Всё остальное здесь — про то, когда повторять запрос, а
когда бессмысленно.
"""

import uuid
from types import SimpleNamespace

import httpx
import pytest

from practix_auth.core.config import settings
from practix_auth.services import notifications_client
from practix_auth.services.notifications_client import emit_user_registered

USER = SimpleNamespace(id=uuid.uuid4(), login='ivan', email='ivan@example.com')


class _FakeClient:
    """Подмена httpx.AsyncClient: считает попытки и отдаёт заготовленные исходы."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.requests: list[tuple[str, dict, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, *, json, headers):
        self.requests.append((url, json, headers))
        outcome = self._outcomes[min(len(self.requests) - 1, len(self._outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, text='')


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, 'NOTIFY_EVENTS_ENABLED', True)
    monkeypatch.setattr(settings, 'NOTIFY_INTAKE_TOKEN', 'test-token')
    monkeypatch.setattr(settings, 'NOTIFY_EVENT_MAX_ATTEMPTS', 3)
    # Без сна: проверяется число попыток, а не длительность пауз.
    monkeypatch.setattr(notifications_client, 'backoff_sleep', _no_sleep)


async def _no_sleep(*args, **kwargs) -> float:
    return 0.0


def _install(monkeypatch, outcomes) -> _FakeClient:
    client = _FakeClient(outcomes)
    monkeypatch.setattr(notifications_client.httpx, 'AsyncClient', lambda **kwargs: client)
    return client


async def test_disabled_flag_makes_no_request(monkeypatch):
    """Без профиля notifications хоста нет в DNS — стучаться туда незачем."""
    monkeypatch.setattr(settings, 'NOTIFY_EVENTS_ENABLED', False)
    client = _install(monkeypatch, [202])

    await emit_user_registered(USER)

    assert client.requests == []


async def test_successful_event_is_sent_once(monkeypatch, enabled):
    client = _install(monkeypatch, [202])

    await emit_user_registered(USER, request_id='req-1')

    assert len(client.requests) == 1
    url, payload, headers = client.requests[0]
    assert url.endswith('/api/v1/notifications/events')
    assert payload['type'] == 'user.registered'
    assert payload['data'] == {'user_id': str(USER.id), 'login': 'ivan', 'email': 'ivan@example.com'}
    assert headers['X-Internal-Token'] == 'test-token'
    assert headers['X-Request-Id'] == 'req-1'


async def test_duplicate_answer_is_not_retried(monkeypatch, enabled):
    """200 «дубликат» — это успех: событие уже принято раньше."""
    client = _install(monkeypatch, [200])

    await emit_user_registered(USER)

    assert len(client.requests) == 1


async def test_client_error_is_not_retried(monkeypatch, enabled):
    """4xx лечится правкой кода, а не повтором того же тела."""
    client = _install(monkeypatch, [400])

    await emit_user_registered(USER)

    assert len(client.requests) == 1


async def test_server_error_is_retried_to_the_limit(monkeypatch, enabled):
    client = _install(monkeypatch, [503])

    await emit_user_registered(USER)

    assert len(client.requests) == settings.NOTIFY_EVENT_MAX_ATTEMPTS


async def test_network_error_is_retried_and_never_propagates(monkeypatch, enabled):
    """Ровно то, ради чего модуль существует: регистрация уже закоммичена."""
    client = _install(monkeypatch, [httpx.ConnectError('connection refused')])

    await emit_user_registered(USER)

    assert len(client.requests) == settings.NOTIFY_EVENT_MAX_ATTEMPTS


async def test_recovery_on_the_second_attempt_stops_retrying(monkeypatch, enabled):
    client = _install(monkeypatch, [httpx.ConnectError('boom'), 202])

    await emit_user_registered(USER)

    assert len(client.requests) == 2


async def test_request_id_is_omitted_when_absent(monkeypatch, enabled):
    client = _install(monkeypatch, [202])

    await emit_user_registered(USER, request_id=None)

    _, _, headers = client.requests[0]
    assert 'X-Request-Id' not in headers


async def test_each_attempt_sends_the_same_event_id(monkeypatch, enabled):
    """Тело строится один раз: перегенерация event_id на повторе — это то, из-за
    чего дедупликация на приёме сделана по природному ключу, а не по нему."""
    client = _install(monkeypatch, [503])

    await emit_user_registered(USER)

    event_ids = {payload['event_id'] for _, payload, _ in client.requests}
    assert len(event_ids) == 1
