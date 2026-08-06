"""Клиент Auth: сетевой отказ обязан остаться внутри таксономии ``Confirm*``.

Ручка редиректа ловит ровно три исключения — ``ConfirmRejected``,
``ConfirmMisconfigured`` и ``ConfirmUnavailable`` — и на каждое отвечает своей
страницей. Всё, что вылетело мимо них, становится необработанной 500: человек с
письмом получает трейсбек вместо ``503.html`` с ``Retry-After``.

Мимо них вылетало два случая, и оба — на самом обычном пути:

* **вход служебной учётки** стоял ВНЕ ``try``. ``_login`` ходит по сети, и
  ``httpx.ConnectError`` из него не перехватывался ничем. Достаточно, чтобы Auth
  лежал в момент, когда холодный воркер обрабатывает первую же ссылку;
* **не-JSON тело**. ``response.json()`` бросает ``ValueError``, а такое тело
  приходит, когда между нами и Auth встал чей-то прокси со своей страницей
  ошибки.
"""

import asyncio
import uuid

import httpx
import pytest

from practix_link_shortener.services.auth_client import AuthClient
from practix_link_shortener.services.exceptions import ConfirmMisconfigured, ConfirmUnavailable


class FakeResponse:
    def __init__(self, status_code: int, payload=None, *, text: str = '') -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError('это не JSON')
        return self._payload


class FakeHttp:
    """Транспорт, отвечающий по сценарию: исключение или готовый ответ."""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls: list[str] = []

    async def post(self, url: str, **kwargs):
        self.calls.append(url)
        item = self.script.pop(0) if self.script else FakeResponse(200, {})
        if isinstance(item, Exception):
            raise item
        return item


def _client(script: list) -> AuthClient:
    client = AuthClient(base_url='http://auth:8000')
    client._client = FakeHttp(script)
    return client


def _confirm(client: AuthClient):
    return asyncio.run(client.confirm_email(uuid.uuid4()))


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    """Бэкофф не должен растягивать набор на секунды."""
    monkeypatch.setattr('practix_link_shortener.services.auth_client.compute_delay', lambda *a, **kw: 0)


def test_network_failure_during_login_is_unavailable_not_a_crash():
    """Главная правка: отказ на входе служебной учётки — это 503, а не 500."""
    client = _client([httpx.ConnectError('connection refused')] * 5)

    with pytest.raises(ConfirmUnavailable):
        _confirm(client)


def test_network_failure_during_login_is_retried():
    """Первая попытка не удалась, вторая прошла — ссылка обязана сработать."""
    client = _client(
        [
            httpx.ConnectError('connection refused'),
            FakeResponse(200, {'access_token': 'token'}),
            FakeResponse(200, {'status': 'confirmed'}),
        ]
    )

    assert _confirm(client) == 'confirmed'


def test_non_json_login_body_is_unavailable():
    client = _client([FakeResponse(200, None, text='<html>502 Bad Gateway</html>')] * 5)

    with pytest.raises(ConfirmUnavailable):
        _confirm(client)


def test_non_json_confirm_body_is_unavailable():
    client = _client([FakeResponse(200, {'access_token': 'token'}), FakeResponse(200, None, text='<html>proxy</html>')])

    with pytest.raises(ConfirmUnavailable):
        _confirm(client)


def test_login_rejection_stays_a_configuration_error():
    """401 на входе — не «подождите», а «почините учётку»: повторять нечего."""
    client = _client([FakeResponse(401)])

    with pytest.raises(ConfirmMisconfigured):
        _confirm(client)


def test_happy_path_talks_to_login_once_and_reuses_the_token():
    client = _client(
        [
            FakeResponse(200, {'access_token': 'token'}),
            FakeResponse(200, {'status': 'confirmed'}),
            FakeResponse(200, {'status': 'already_confirmed'}),
        ]
    )

    assert _confirm(client) == 'confirmed'
    assert _confirm(client) == 'already_confirmed'
    assert sum('auth/login' in url for url in client._client.calls) == 1
