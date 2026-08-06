"""Проверка токена кабинета: подпись локально, отзыв — в Redis сервиса Auth.

Сетевого вызова в Auth здесь нет намеренно: рассылка, которая на каждый запрос
ходит в Auth, кладёт Auth, а вместе с ним логин на всём сайте. Секрет общий,
подпись проверяется на месте — ровно как в коллекторе и UGC API.
"""

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from practix_notifications.core import jwt_auth
from practix_notifications.core.config import settings

SUBJECT = '11111111-1111-1111-1111-111111111111'


def _token(**overrides) -> str:
    payload = {
        'sub': SUBJECT,
        'jti': 'jti-1',
        'type': 'access',
        'roles': ['user'],
        'exp': datetime.now(UTC) + timedelta(minutes=5),
    }
    payload.update(overrides)
    secret = overrides.pop('_secret', settings.AUTHJWT_SECRET_KEY)
    return jwt.encode(payload, secret, algorithm=settings.NOTIFY_JWT_ALGORITHM)


class _FakeRedis:
    def __init__(self, *, revoked: set[str] | None = None, error: Exception | None = None):
        self._revoked = revoked or set()
        self._error = error
        self.calls = 0

    def get(self, key: str):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return 'revoked' if key in self._revoked else None


@pytest.fixture(autouse=True)
def _no_denylist(monkeypatch):
    """По умолчанию денилист выключен — тесты подписи не должны ходить в Redis."""
    monkeypatch.setattr(settings, 'NOTIFY_DENYLIST_ENABLED', False)


def test_valid_token_yields_subject_and_jti():
    claims = jwt_auth.decode_access_token(_token())

    assert claims.subject == SUBJECT
    assert claims.jti == 'jti-1'
    assert claims.roles == ('user',)


def test_token_signed_with_another_secret_is_rejected():
    forged = jwt.encode({'sub': SUBJECT, 'jti': 'x', 'type': 'access'}, 'другой-секрет', algorithm='HS256')

    with pytest.raises(jwt_auth.TokenError):
        jwt_auth.decode_access_token(forged)


def test_expired_token_is_rejected():
    with pytest.raises(jwt_auth.TokenError):
        jwt_auth.decode_access_token(_token(exp=datetime.now(UTC) - timedelta(hours=1)))


def test_refresh_token_does_not_open_the_cabinet():
    """Refresh живёт неделями: открывать им ленту значило бы обесценить
    короткий срок жизни access-токена."""
    with pytest.raises(jwt_auth.TokenError):
        jwt_auth.decode_access_token(_token(type='refresh'))


def test_token_without_jti_is_rejected():
    """Без jti нечего проверять в денилисте — отозвать такой токен невозможно."""
    with pytest.raises(jwt_auth.TokenError):
        jwt_auth.decode_access_token(_token(jti=None))


def test_malformed_token_is_rejected():
    with pytest.raises(jwt_auth.TokenError):
        jwt_auth.decode_access_token('не.токен.вовсе')


def test_bearer_token_is_read_from_the_header(rf):
    request = rf.get('/', headers={'authorization': 'Bearer abc'})
    assert jwt_auth.bearer_token(request) == 'abc'


@pytest.mark.parametrize('header', ['', 'abc', 'Basic abc', 'Bearer '])
def test_non_bearer_headers_give_nothing(rf, header):
    request = rf.get('/', headers={'authorization': header})
    assert jwt_auth.bearer_token(request) is None


def test_denylist_disabled_does_not_touch_redis(monkeypatch):
    fake = _FakeRedis(revoked={'jti-1'})
    monkeypatch.setattr(jwt_auth, 'get_auth_redis', lambda: fake)

    assert jwt_auth.is_revoked('jti-1') is False
    assert fake.calls == 0


def test_revoked_token_is_detected(monkeypatch):
    monkeypatch.setattr(settings, 'NOTIFY_DENYLIST_ENABLED', True)
    monkeypatch.setattr(jwt_auth, 'get_auth_redis', lambda: _FakeRedis(revoked={'jti-1'}))

    assert jwt_auth.is_revoked('jti-1') is True
    assert jwt_auth.is_revoked('jti-2') is False


@pytest.mark.parametrize(('policy', 'expected'), [('deny', True), ('allow', False)])
def test_denylist_failure_policy(monkeypatch, policy, expected):
    monkeypatch.setattr(settings, 'NOTIFY_DENYLIST_ENABLED', True)
    monkeypatch.setattr(settings, 'NOTIFY_DENYLIST_ON_ERROR', policy)
    monkeypatch.setattr(jwt_auth, 'get_auth_redis', lambda: _FakeRedis(error=OSError('redis is down')))

    assert jwt_auth.is_revoked('jti-1') is expected


def test_denylist_raise_policy_propagates(monkeypatch):
    monkeypatch.setattr(settings, 'NOTIFY_DENYLIST_ENABLED', True)
    monkeypatch.setattr(settings, 'NOTIFY_DENYLIST_ON_ERROR', 'raise')
    monkeypatch.setattr(jwt_auth, 'get_auth_redis', lambda: _FakeRedis(error=OSError('redis is down')))

    with pytest.raises(OSError):
        jwt_auth.is_revoked('jti-1')


def test_authenticate_rejects_a_revoked_token(rf, monkeypatch):
    monkeypatch.setattr(settings, 'NOTIFY_DENYLIST_ENABLED', True)
    monkeypatch.setattr(jwt_auth, 'get_auth_redis', lambda: _FakeRedis(revoked={'jti-1'}))
    request = rf.get('/', headers={'authorization': f'Bearer {_token()}'})

    with pytest.raises(jwt_auth.TokenError, match='revoked'):
        jwt_auth.authenticate(request)


def test_authenticate_without_a_header_is_an_error(rf):
    with pytest.raises(jwt_auth.TokenError):
        jwt_auth.authenticate(rf.get('/'))
