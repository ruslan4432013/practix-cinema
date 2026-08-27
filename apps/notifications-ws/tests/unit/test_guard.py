"""Три рубежа перед потоком уведомлений: Origin, срок токена, денилист."""

import time

import pytest

from practix_notifications_ws.core.config import settings
from practix_notifications_ws.services import guard
from practix_notifications_ws.services.tickets import TicketPayload

LIVE = TicketPayload(subject='user-1', jti='jti-1', expires_at=4_102_444_800)


# --- Origin ------------------------------------------------------------------


def test_allowed_origin_passes():
    assert guard.origin_allowed(settings.allowed_origins[0])


def test_foreign_origin_is_rejected():
    """Cross-Site WebSocket Hijacking: у сокета нет same-origin policy, и без
    этой проверки чужая страница открыла бы поток к нам сама."""
    assert not guard.origin_allowed('https://evil.example')


def test_missing_origin_follows_the_flag(monkeypatch):
    """Запрос без Origin браузером не отправлен — так приходят websocat, curl и
    функциональные тесты. Защищать их от CSWSH нечего."""
    monkeypatch.setattr(settings, 'NOTIFY_WS_ALLOW_MISSING_ORIGIN', True)
    assert guard.origin_allowed(None)

    monkeypatch.setattr(settings, 'NOTIFY_WS_ALLOW_MISSING_ORIGIN', False)
    assert not guard.origin_allowed(None)


# --- срок токена -------------------------------------------------------------


def test_expired_token_is_detected():
    stale = TicketPayload(subject='user-1', jti='jti-1', expires_at=int(time.time()) - 1)
    assert guard.token_expired(stale)


def test_live_token_is_not_expired():
    assert not guard.token_expired(LIVE)


def test_token_without_exp_never_expires():
    """``exp = 0`` означает «в токене срока не было». Придумывать его за Auth
    шлюз не должен: он проверяет чужие токены, а не выпускает свои."""
    assert not guard.token_expired(TicketPayload(subject='user-1', jti='jti-1', expires_at=0))


# --- денилист ----------------------------------------------------------------


async def test_revoked_token_is_detected(redis):
    redis.data['jti-1'] = 'revoked'
    assert await guard.token_revoked(redis, 'jti-1')


async def test_active_token_is_not_revoked(redis):
    assert not await guard.token_revoked(redis, 'jti-1')


@pytest.mark.parametrize(
    ('policy', 'expected'),
    [('deny', True), ('allow', False)],
)
async def test_denylist_failure_follows_the_policy(redis, monkeypatch, policy, expected):
    """Политика — параметр, а не общий дефолт (см. practix_core.jwt).

    Здесь она 'deny': держать открытым поток уведомлений по токену, про который
    неизвестно, жив ли он, хуже, чем закрыть соединение.
    """
    monkeypatch.setattr(settings, 'NOTIFY_WS_DENYLIST_ON_ERROR', policy)
    redis.explode = ConnectionError('redis is down')

    assert await guard.token_revoked(redis, 'jti-1') is expected


async def test_denylist_failure_can_raise(redis, monkeypatch):
    monkeypatch.setattr(settings, 'NOTIFY_WS_DENYLIST_ON_ERROR', 'raise')
    redis.explode = ConnectionError('redis is down')

    with pytest.raises(ConnectionError):
        await guard.token_revoked(redis, 'jti-1')


async def test_missing_client_is_treated_as_a_failure(monkeypatch):
    monkeypatch.setattr(settings, 'NOTIFY_WS_DENYLIST_ON_ERROR', 'deny')
    assert await guard.token_revoked(None, 'jti-1')


# --- совокупная проверка -----------------------------------------------------


async def test_still_valid_covers_both_reasons(redis):
    assert await guard.still_valid(redis, LIVE)

    redis.data['jti-1'] = 'revoked'
    assert not await guard.still_valid(redis, LIVE)


async def test_expired_token_short_circuits_the_denylist(redis):
    """Истёкший токен не повод ходить в Redis: ответ уже известен."""
    redis.explode = ConnectionError('redis is down')
    stale = TicketPayload(subject='user-1', jti='jti-1', expires_at=1)

    assert not await guard.still_valid(redis, stale)
