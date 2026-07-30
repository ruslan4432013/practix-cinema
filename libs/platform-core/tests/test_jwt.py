"""Проверки обвязки JWT, прежде всего — политики при недоступности Redis.

Этот кластер единственный, где план предлагал СМЕНИТЬ поведение (сделать
fail-open во всех сервисах). Для Auth это было бы регрессией: он ведёт денилист,
и принять токен вышедшего пользователя при сбое Redis означает, что logout
перестаёт держать. Тесты ниже фиксируют, что три политики различимы и что
поведение по умолчанию — прежнее (``raise``).
"""

import pytest
from async_fastapi_jwt_auth import AuthJWT

from practix_core.jwt import install_denylist_loader, make_jwt_settings

TOKEN = {'jti': 'jti-1'}


class _Redis:
    def __init__(self, value=None, boom: bool = False):
        self.value = value
        self.boom = boom

    async def get(self, key):
        if self.boom:
            raise ConnectionError('redis is down')
        return self.value


def _loader(provider, **kwargs):
    """Регистрирует загрузчик и возвращает его для прямого вызова."""
    install_denylist_loader(provider, **kwargs)
    return AuthJWT._token_in_denylist_callback


# --- make_jwt_settings ------------------------------------------------------


def test_only_requested_fields_are_present():
    """Лишнее поле со значением None перезаписало бы дефолт библиотеки."""
    model = make_jwt_settings(
        authjwt_secret_key='s',
        authjwt_denylist_enabled=True,
        authjwt_denylist_token_checks={'access'},
    )
    dumped = model.model_dump()
    assert set(dumped) == {'authjwt_secret_key', 'authjwt_denylist_enabled', 'authjwt_denylist_token_checks'}
    assert 'authjwt_access_token_expires' not in dumped


def test_expiry_fields_are_included_when_given():
    """Auth, в отличие от остальных, выпускает токены и задаёт сроки."""
    model = make_jwt_settings(
        authjwt_secret_key='s',
        authjwt_denylist_enabled=True,
        authjwt_denylist_token_checks={'access', 'refresh'},
        authjwt_access_token_expires=3600,
        authjwt_refresh_token_expires=2592000,
    )
    assert model.authjwt_access_token_expires == 3600
    assert model.authjwt_refresh_token_expires == 2592000


def test_cli_style_minimal_settings():
    """Четвёртая копия (auth/src/cli.py) задавала только секрет."""
    assert make_jwt_settings(authjwt_secret_key='s').model_dump() == {'authjwt_secret_key': 's'}


def test_unknown_field_is_rejected_loudly():
    with pytest.raises(ValueError, match='Неизвестные поля'):
        make_jwt_settings(authjwt_secret_key='s', authjwt_typo=True)


# --- денилист: нормальная работа --------------------------------------------


async def test_revoked_token_is_detected():
    loader = _loader(lambda: _Redis(value='revoked'))
    assert await loader(TOKEN) is True


async def test_live_token_is_not_revoked():
    loader = _loader(lambda: _Redis(value=None))
    assert await loader(TOKEN) is False


async def test_async_provider_is_awaited():
    """Auth отдаёт клиент асинхронной функцией get_redis()."""

    async def provider():
        return _Redis(value='revoked')

    assert await _loader(provider)(TOKEN) is True


# --- денилист: политика при отказе ------------------------------------------


async def test_raise_is_the_default_policy():
    """Прежнее поведение rest/auth сохранено по умолчанию."""
    loader = _loader(lambda: _Redis(boom=True))
    with pytest.raises(ConnectionError):
        await loader(TOKEN)


async def test_allow_policy_treats_failure_as_not_revoked():
    """Выбор коллектора: событие важнее, риск ограничен."""
    loader = _loader(lambda: _Redis(boom=True), on_error='allow')
    assert await loader(TOKEN) is False


async def test_deny_policy_treats_failure_as_revoked():
    loader = _loader(lambda: _Redis(boom=True), on_error='deny')
    assert await loader(TOKEN) is True


async def test_missing_client_raises_under_default_policy():
    """rest держит клиент в модульной переменной: до lifespan он None."""
    loader = _loader(lambda: None)
    with pytest.raises(RuntimeError, match='not initialised'):
        await loader(TOKEN)


async def test_missing_client_is_tolerated_under_allow():
    loader = _loader(lambda: None, on_error='allow')
    assert await loader(TOKEN) is False


async def test_provider_is_called_lazily_per_request():
    """Клиент создаётся в lifespan позже импорта — провайдер обязан быть ленивым."""
    calls = {'n': 0}
    box: dict = {'client': None}

    def provider():
        calls['n'] += 1
        return box['client']

    loader = _loader(provider, on_error='allow')
    assert await loader(TOKEN) is False  # клиента ещё нет
    box['client'] = _Redis(value='revoked')
    assert await loader(TOKEN) is True  # появился — и сразу учтён
    assert calls['n'] == 2
