"""Резолв личности: пачки, кеш и то, что промахи не кешируются."""

import pytest

from practix_notifications.core.config import settings
from practix_notifications.services import directory as directory_module
from practix_notifications.services.auth_client import AuthUnavailable
from practix_notifications.services.directory import Directory, Person, template_vars


class FakeAuth:
    """Клиент Auth, помнящий, о ком его спрашивали."""

    def __init__(self, users: dict[str, dict] | None = None) -> None:
        self.users = users or {}
        self.calls: list[list[str]] = []
        self.fail_with: Exception | None = None

    def lookup_users(self, ids: list[str]) -> dict[str, dict]:
        self.calls.append(list(ids))
        if self.fail_with is not None:
            raise self.fail_with
        return {user_id: self.users[user_id] for user_id in ids if user_id in self.users}


def _user(user_id: str, **overrides) -> dict:
    payload = {
        'id': user_id,
        'login': f'user{user_id}',
        'email': f'{user_id}@example.com',
        'first_name': 'Иван',
        'last_name': 'Петров',
    }
    payload.update(overrides)
    return payload


def test_lookup_is_split_into_batches(monkeypatch):
    """Пачками, а не по получателю: иначе рассылка кладёт Auth, а с ним и вход на сайт."""
    monkeypatch.setattr(settings, 'NOTIFY_BUILDER_LOOKUP_BATCH', 2)
    monkeypatch.setattr(settings, 'NOTIFY_DIRECTORY_CACHE_TTL', 0)
    client = FakeAuth({str(index): _user(str(index)) for index in range(5)})

    resolved = Directory(client).resolve([str(index) for index in range(5)])

    assert len(resolved) == 5
    assert [len(call) for call in client.calls] == [2, 2, 1]


def test_duplicates_are_asked_about_once():
    client = FakeAuth({'1': _user('1')})

    Directory(client).resolve(['1', '1', '1'])

    assert client.calls == [['1']]


def test_second_resolve_is_served_from_cache(monkeypatch):
    monkeypatch.setattr(settings, 'NOTIFY_DIRECTORY_CACHE_TTL', 300)
    client = FakeAuth({'1': _user('1')})
    directory = Directory(client)

    directory.resolve(['1'])
    resolved = directory.resolve(['1'])

    assert len(client.calls) == 1
    assert resolved['1'].email == '1@example.com'


def test_expired_cache_entry_is_refetched(monkeypatch):
    monkeypatch.setattr(settings, 'NOTIFY_DIRECTORY_CACHE_TTL', 300)
    client = FakeAuth({'1': _user('1')})
    directory = Directory(client)
    directory.resolve(['1'])

    # Часы подкручиваются вперёд от реального значения, а не тест ждёт пять
    # минут по-настоящему. Именно вперёд: monotonic на разных системах считает
    # от разных точек, и абсолютная константа может оказаться в прошлом.
    later = directory_module.time.monotonic() + 10_000
    monkeypatch.setattr(directory_module.time, 'monotonic', lambda: later)
    directory.resolve(['1'])

    assert len(client.calls) == 2


def test_misses_are_never_cached(monkeypatch):
    """Зарегистрировавшийся секунду назад не должен быть невидим пять минут.

    Именно ему уходит приветственное письмо — то самое, ради которого сервис и
    узнаёт о регистрации мгновенно, а не следующей синхронизацией.
    """
    monkeypatch.setattr(settings, 'NOTIFY_DIRECTORY_CACHE_TTL', 300)
    client = FakeAuth({})
    directory = Directory(client)

    assert directory.resolve(['1']) == {}
    client.users['1'] = _user('1')

    assert '1' in directory.resolve(['1'])


def test_cache_is_bounded(monkeypatch):
    """Воркер живёт неделями: рассылка на миллион адресов не должна остаться в памяти."""
    monkeypatch.setattr(settings, 'NOTIFY_DIRECTORY_CACHE_TTL', 300)
    monkeypatch.setattr(settings, 'NOTIFY_DIRECTORY_CACHE_MAX', 2)
    client = FakeAuth({str(index): _user(str(index)) for index in range(5)})
    directory = Directory(client)

    directory.resolve([str(index) for index in range(5)])

    assert len(directory._cache) <= 2


def test_auth_failure_propagates():
    """Решение «подождать всей пачкой» принимает воркер, а не резолвер."""
    client = FakeAuth()
    client.fail_with = AuthUnavailable('Auth ответил 503')

    with pytest.raises(AuthUnavailable):
        Directory(client).resolve(['1'])


@pytest.mark.parametrize(
    ('first', 'last', 'full', 'display'),
    [
        ('Иван', 'Петров', 'Иван Петров', 'Иван'),
        ('Иван', '', 'Иван', 'Иван'),
        ('', 'Петров', 'Петров', 'Петров'),
        ('', '', 'ivan', 'ivan'),
    ],
)
def test_name_fallbacks(first, last, full, display):
    """«Здравствуйте, !» хуже, чем обращение по нику."""
    person = Person(user_id='1', login='ivan', email='i@e.c', first_name=first, last_name=last)

    assert person.full_name == full
    assert person.display_name == display


def test_all_template_keys_are_always_present():
    """StrictUndefined: отсутствующая переменная — это не пустое место, а неотправленное письмо."""
    person = Person.from_auth({'id': '1', 'login': 'ivan', 'email': 'i@e.c'})

    variables = template_vars(person, unsubscribe_url='https://x/y')

    assert set(variables) == {
        'login',
        'email',
        'first_name',
        'last_name',
        'full_name',
        'display_name',
        'unsubscribe_url',
        'confirm_url',
    }
    # None из Auth превращается в пустую строку: иначе в письме появилось бы
    # слово «None».
    assert variables['first_name'] == ''
    assert all(isinstance(value, str) for value in variables.values())
