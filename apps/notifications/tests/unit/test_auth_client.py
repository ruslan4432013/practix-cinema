"""Клиент Auth: заголовки, повторы и разница между «сейчас» и «навсегда»."""

import pytest

from practix_notifications.services import auth_client as auth_client_module
from practix_notifications.services.auth_client import AuthClient, AuthClientError, AuthUnavailable


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, headers: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = str(self._payload)

    def json(self) -> dict:
        return self._payload


@pytest.fixture
def captured(monkeypatch):
    """Перехват requests и sleep: тест не ходит в сеть и не ждёт по-настоящему."""
    calls: dict[str, list] = {'requests': [], 'sleeps': []}
    responses: list[FakeResponse] = []

    def fake_request(method, url, **kwargs):
        calls['requests'].append({'method': method, 'url': url, **kwargs})
        return responses.pop(0)

    monkeypatch.setattr(auth_client_module.requests, 'request', fake_request)
    monkeypatch.setattr(auth_client_module.time, 'sleep', lambda delay: calls['sleeps'].append(delay))
    calls['responses'] = responses
    return calls


def test_request_id_is_sent_even_without_a_request_context(captured):
    """Иначе Auth отвечает 400 на КАЖДЫЙ вызов из воркера.

    RequestIdMiddleware в Auth работает в режиме reject_400, а в воркере и в
    management-команде ContextVar пуст: заголовок обязан генерироваться здесь.
    """
    captured['responses'].append(FakeResponse(200, {'ok': True}))

    AuthClient(base_url='http://auth:8000')._request('GET', '/ping')

    assert captured['requests'][0]['headers']['X-Request-Id']


def test_rate_limit_is_retried_and_retry_after_is_honoured(captured, monkeypatch):
    """429 — штатный ответ Auth формирующему воркеру, а не повод сдаться."""
    monkeypatch.setattr(auth_client_module.settings, 'NOTIFY_AUTH_MAX_ATTEMPTS', 2)
    captured['responses'].append(FakeResponse(429, headers={'Retry-After': '7'}))
    captured['responses'].append(FakeResponse(200, {'ok': True}))

    result = AuthClient(base_url='http://auth:8000')._request('GET', '/ping')

    assert result == {'ok': True}
    assert captured['sleeps'] == [7]


def test_retry_after_is_capped(captured, monkeypatch):
    """Чужой заголовок не должен парковать воркер на час."""
    monkeypatch.setattr(auth_client_module.settings, 'NOTIFY_AUTH_MAX_ATTEMPTS', 2)
    monkeypatch.setattr(auth_client_module.settings, 'NOTIFY_AUTH_RETRY_AFTER_MAX', 5)
    captured['responses'].append(FakeResponse(429, headers={'Retry-After': '3600'}))
    captured['responses'].append(FakeResponse(200, {}))

    AuthClient(base_url='http://auth:8000')._request('GET', '/ping')

    assert captured['sleeps'] == [5]


def test_unparsable_retry_after_falls_back_to_backoff(captured, monkeypatch):
    """Retry-After умеет быть датой по RFC 7231. Разбирать её незачем — есть backoff."""
    monkeypatch.setattr(auth_client_module.settings, 'NOTIFY_AUTH_MAX_ATTEMPTS', 2)
    captured['responses'].append(FakeResponse(429, headers={'Retry-After': 'Wed, 21 Oct 2026 07:28:00 GMT'}))
    captured['responses'].append(FakeResponse(200, {}))

    AuthClient(base_url='http://auth:8000')._request('GET', '/ping')

    assert captured['sleeps'] == [pytest.approx(0.1)]


def test_exhausted_attempts_are_temporary(captured, monkeypatch):
    """AuthUnavailable, а не AuthClientError: пачка обязана уехать в retry."""
    monkeypatch.setattr(auth_client_module.settings, 'NOTIFY_AUTH_MAX_ATTEMPTS', 2)
    captured['responses'].extend([FakeResponse(503), FakeResponse(503)])

    with pytest.raises(AuthUnavailable):
        AuthClient(base_url='http://auth:8000')._request('GET', '/ping')


def test_forbidden_is_permanent_and_not_retried(captured):
    """Отобранная роль или не тот пароль не лечатся десятью повторами на пачку."""
    captured['responses'].append(FakeResponse(403, {'detail': 'forbidden'}))

    with pytest.raises(AuthClientError) as exc:
        AuthClient(base_url='http://auth:8000')._request('GET', '/ping', authenticated=False)

    assert not isinstance(exc.value, AuthUnavailable)
    assert len(captured['requests']) == 1


def test_lookup_returns_a_map_and_ignores_missing(captured):
    captured['responses'].append(
        FakeResponse(200, {'items': [{'id': 'u1', 'login': 'ivan', 'email': 'i@e.c'}], 'missing': ['u2']})
    )
    client = AuthClient(base_url='http://auth:8000')
    client._token = 'token'

    found = client.lookup_users(['u1', 'u2'])

    assert set(found) == {'u1'}
    assert captured['requests'][0]['json'] == {'ids': ['u1', 'u2']}


def test_empty_lookup_does_not_touch_the_network(captured):
    assert AuthClient(base_url='http://auth:8000').lookup_users([]) == {}
    assert captured['requests'] == []


def test_lookup_skips_items_without_an_id(captured):
    """Кривой элемент пропускается, а не роняет разбор всей пачки.

    ``item['id']`` давал бы ``KeyError`` — исключение, которого нет в таксономии
    клиента. Формирующий воркер ловит только ``AuthUnavailable`` и
    ``AuthClientError``, поэтому оно доходило бы до общего обработчика
    консьюмера, превращалось в ``RETRY`` и сжигало весь бюджет сборки на ответе,
    который повтором не чинится.
    """
    captured['responses'].append(
        FakeResponse(200, {'items': [{'login': 'без-id'}, {'id': None}, {'id': 'u1', 'login': 'ivan'}]})
    )
    client = AuthClient(base_url='http://auth:8000')
    client._token = 'token'

    found = client.lookup_users(['u1'])

    assert set(found) == {'u1'}
