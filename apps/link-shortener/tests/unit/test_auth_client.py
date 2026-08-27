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


class RoutingHttp:
    """Транспорт, отвечающий по URL, а не по очереди.

    Сценарный ``FakeHttp`` для гонки не годится: при параллельных переходах
    порядок вызовов не определён, и очередь начала бы отдавать ответ входа на
    подтверждение. Здесь же ``await asyncio.sleep(0)`` внутри ``post`` — точка
    переключения, без которой корутины отработали бы по одной и гонки бы просто
    не случилось.
    """

    def __init__(self, *, token: str = 'fresh', expired: str | None = None) -> None:
        self._token = token
        self._expired = expired
        self.logins = 0
        self.confirms = 0

    async def post(self, url: str, **kwargs):
        await asyncio.sleep(0)
        if 'auth/login' in url:
            self.logins += 1
            return FakeResponse(200, {'access_token': self._token})

        self.confirms += 1
        authorization = (kwargs.get('headers') or {}).get('Authorization')
        if self._expired is not None and authorization == f'Bearer {self._expired}':
            return FakeResponse(401)
        return FakeResponse(200, {'status': 'confirmed'})


def _client(script: list) -> AuthClient:
    client = AuthClient(base_url='http://auth:8000')
    client._client = FakeHttp(script)
    return client


def _gather_confirms(client: AuthClient, count: int) -> list[str]:
    async def run():
        return await asyncio.gather(*(client.confirm_email(uuid.uuid4()) for _ in range(count)))

    return asyncio.run(run())


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


def test_concurrent_cold_start_logs_in_once():
    """Холодный клиент под пачкой переходов входит один раз, а не пять.

    Клиент — синглтон на процесс, и до замка каждая корутина видела
    ``self._token`` пустым и уходила логиниться сама.
    """
    client = AuthClient(base_url='http://auth:8000')
    client._client = RoutingHttp()

    assert _gather_confirms(client, 5) == ['confirmed'] * 5
    assert client._client.logins == 1
    assert client._client.confirms == 5


def test_expiry_under_load_relogins_once():
    """Протухание под нагрузкой стоит одного повторного входа, а не пяти.

    Это тест на ``stale``: одного замка мало — опоздавшая корутина с 401 на
    старом токене обнулением затёрла бы уже полученный свежий и запустила бы
    второй круг входов.
    """
    client = AuthClient(base_url='http://auth:8000')
    client._client = RoutingHttp(token='fresh', expired='old')
    client._token = 'old'

    assert _gather_confirms(client, 5) == ['confirmed'] * 5
    assert client._client.logins == 1
    assert client._token == 'fresh'
