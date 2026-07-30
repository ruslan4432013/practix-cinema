from urllib.parse import parse_qs, urlparse

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from core.config import settings
from services import oauth_providers
from services.oauth_providers import YandexProvider

# Роутер OAuth монтируется только при OAUTH_ENABLED=True. Если фича выключена
# в окружении, пропускаем весь модуль (в функциональном docker-compose флаг задан).
pytestmark = pytest.mark.skipif(
    not settings.OAUTH_ENABLED,
    reason='OAUTH_ENABLED выключен — роутер OAuth не смонтирован',
)


class FakeYandexProvider(YandexProvider):
    """Подмена YandexProvider — без реальных HTTP-запросов к Яндексу.

    build_authorize_url и extract_profile берутся настоящие, подменяются только
    сетевые вызовы обмена кода и получения профиля.
    """

    def __init__(self, userinfo: dict):
        super().__init__()
        self.userinfo = userinfo

    async def exchange_code(self, code: str) -> dict:
        return {'access_token': 'fake-access-token', 'refresh_token': 'fake-refresh-token'}

    async def get_user_info(self, access_token: str) -> dict:
        return self.userinfo


@pytest.fixture(autouse=True)
def _restore_registry():
    """Восстанавливает реестр провайдеров после каждого теста."""
    original = dict(oauth_providers._REGISTRY)
    yield
    oauth_providers._REGISTRY.clear()
    oauth_providers._REGISTRY.update(original)


def _use_fake_yandex(userinfo: dict):
    """Подменяет провайдера yandex в реестре фейковой реализацией."""
    oauth_providers._REGISTRY['yandex'] = FakeYandexProvider(userinfo)


async def _obtain_state(client: AsyncClient) -> str:
    """Проходит /login и достаёт сгенерированный state из Location."""
    resp = await client.get('/api/v1/oauth/yandex/login', follow_redirects=False)
    assert resp.status_code == 307
    location = resp.headers['location']
    query = parse_qs(urlparse(location).query)
    return query['state'][0]


@pytest.mark.asyncio
async def test_yandex_login_redirect(client: AsyncClient):
    """/login отдаёт 307 на Яндекс и кладёт state в Redis."""
    from db.redis import get_redis

    resp = await client.get('/api/v1/oauth/yandex/login', follow_redirects=False)
    assert resp.status_code == 307
    location = resp.headers['location']
    assert location.startswith('https://oauth.yandex.ru/authorize')
    query = parse_qs(urlparse(location).query)
    assert query['response_type'] == ['code']
    state = query['state'][0]

    redis = await get_redis()
    assert await redis.get(f'oauth:state:{state}') is not None


@pytest.mark.asyncio
async def test_yandex_callback_new_user(client: AsyncClient):
    """Первый вход через Яндекс создаёт пользователя и связь social_accounts."""
    from conftest import TestingSessionLocal

    _use_fake_yandex({'id': '42', 'login': 'vasya', 'default_email': 'vasya@ya.ru'})
    state = await _obtain_state(client)

    resp = await client.get(f'/api/v1/oauth/yandex/callback?code=abc&state={state}')
    assert resp.status_code == 200
    body = resp.json()
    assert 'access_token' in body
    assert 'refresh_token' in body

    async with TestingSessionLocal() as session:
        users = await session.execute(text("SELECT id FROM users WHERE email = 'vasya@ya.ru'"))
        assert len(users.fetchall()) == 1
        socials = await session.execute(
            text("SELECT id FROM social_accounts WHERE provider = 'yandex' AND provider_user_id = '42'")
        )
        assert len(socials.fetchall()) == 1


@pytest.mark.asyncio
async def test_yandex_callback_existing_social_user(client: AsyncClient):
    """Повторный вход того же Яндекс-пользователя не плодит записи."""
    from conftest import TestingSessionLocal

    _use_fake_yandex({'id': '77', 'login': 'petya', 'default_email': 'petya@ya.ru'})

    first = await client.get(f'/api/v1/oauth/yandex/callback?code=c1&state={await _obtain_state(client)}')
    assert first.status_code == 200
    second = await client.get(f'/api/v1/oauth/yandex/callback?code=c2&state={await _obtain_state(client)}')
    assert second.status_code == 200

    async with TestingSessionLocal() as session:
        users = await session.execute(text("SELECT id FROM users WHERE email = 'petya@ya.ru'"))
        assert len(users.fetchall()) == 1
        socials = await session.execute(text("SELECT id FROM social_accounts WHERE provider_user_id = '77'"))
        assert len(socials.fetchall()) == 1


@pytest.mark.asyncio
async def test_yandex_callback_link_by_email(client: AsyncClient):
    """Если email совпадает с локальным аккаунтом — связываем, а не дублируем."""
    from conftest import TestingSessionLocal

    # Локальная регистрация.
    reg = await client.post(
        '/api/v1/auth/register',
        json={'login': 'localuser', 'email': 'shared@ya.ru', 'password': 'Password123!'},
    )
    assert reg.status_code == 201
    local_user_id = reg.json()['id']

    _use_fake_yandex({'id': '99', 'login': 'shared', 'default_email': 'shared@ya.ru'})
    resp = await client.get(f'/api/v1/oauth/yandex/callback?code=abc&state={await _obtain_state(client)}')
    assert resp.status_code == 200

    async with TestingSessionLocal() as session:
        users = await session.execute(text("SELECT id FROM users WHERE email = 'shared@ya.ru'"))
        rows = users.fetchall()
        assert len(rows) == 1  # новый пользователь не создан

        social = await session.execute(text("SELECT user_id FROM social_accounts WHERE provider_user_id = '99'"))
        social_rows = social.fetchall()
        assert len(social_rows) == 1
        assert str(social_rows[0][0]) == local_user_id  # связь ведёт на локальный аккаунт


@pytest.mark.asyncio
async def test_yandex_callback_no_email(client: AsyncClient):
    """Пользователь Яндекса без email получает синтетический @yandex.local."""
    from conftest import TestingSessionLocal

    _use_fake_yandex({'id': '500', 'login': 'noemail'})
    resp = await client.get(f'/api/v1/oauth/yandex/callback?code=abc&state={await _obtain_state(client)}')
    assert resp.status_code == 200

    async with TestingSessionLocal() as session:
        res = await session.execute(
            text("""
                SELECT u.email FROM users u
                JOIN social_accounts s ON s.user_id = u.id
                WHERE s.provider_user_id = '500'
            """)
        )
        email = res.scalar_one()
        assert email.endswith('@yandex.local')


@pytest.mark.asyncio
async def test_yandex_callback_bad_state(client: AsyncClient):
    """Неизвестный/просроченный state → 400."""
    _use_fake_yandex({'id': '1', 'login': 'x', 'default_email': 'x@ya.ru'})
    resp = await client.get('/api/v1/oauth/yandex/callback?code=abc&state=nonexistent')
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_yandex_callback_provider_error(client: AsyncClient):
    """Яндекс вернул ошибку (пользователь отказал) → 400 с описанием."""
    state = await _obtain_state(client)
    resp = await client.get(f'/api/v1/oauth/yandex/callback?error=access_denied&error_description=denied&state={state}')
    assert resp.status_code == 400
    assert 'denied' in resp.json()['detail']
