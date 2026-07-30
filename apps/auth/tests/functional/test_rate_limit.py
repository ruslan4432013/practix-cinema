import pytest
from httpx import AsyncClient

from practix_auth.core.config import settings

# Тестовые креды заведомо неверные — вход вернёт 401, но каждый запрос всё равно
# учитывается rate limit'ом (middleware отрабатывает до обработчика ручки).
_LOGIN_PAYLOAD = {'login': 'nobody', 'password': 'wrong-password'}


@pytest.mark.asyncio
async def test_rate_limit_returns_429_after_limit(client: AsyncClient, monkeypatch):
    """После превышения лимита запросов сервер отвечает 429 Too Many Requests."""
    limit = 3
    monkeypatch.setattr(settings, 'RATE_LIMIT_ENABLED', True)
    monkeypatch.setattr(settings, 'RATE_LIMIT_TIMES', limit)
    monkeypatch.setattr(settings, 'RATE_LIMIT_SECONDS', 60)

    # Первые `limit` запросов проходят (401 за неверные креды), но не 429.
    for _ in range(limit):
        response = await client.post('/api/v1/auth/login', json=_LOGIN_PAYLOAD)
        assert response.status_code != 429

    # Следующий запрос упирается в лимит.
    response = await client.post('/api/v1/auth/login', json=_LOGIN_PAYLOAD)
    assert response.status_code == 429
    assert response.json()['detail'] == 'Too Many Requests'
    assert 'retry-after' in {k.lower() for k in response.headers}


@pytest.mark.asyncio
async def test_rate_limit_disabled_never_throttles(client: AsyncClient, monkeypatch):
    """При выключенном rate limit запросы не отбрасываются с 429."""
    monkeypatch.setattr(settings, 'RATE_LIMIT_ENABLED', False)
    monkeypatch.setattr(settings, 'RATE_LIMIT_TIMES', 3)

    for _ in range(10):
        response = await client.post('/api/v1/auth/login', json=_LOGIN_PAYLOAD)
        assert response.status_code != 429


@pytest.mark.asyncio
async def test_spoofed_forwarded_for_does_not_reset_counter(client: AsyncClient, monkeypatch):
    """Подделанный ``X-Forwarded-For`` не даёт клиенту новый счётчик лимита.

    Nginx формирует заголовок как ``$proxy_add_x_forwarded_for``: слева — то, что
    прислал клиент, справа — дописанный прокси настоящий адрес. Здесь это
    воспроизводится вручную: левый элемент каждый раз новый (его «подделывает»
    клиент), правый постоянный.

    Регрессия на реальную уязвимость: раньше ``get_client_ip`` брал ПЕРВЫЙ хоп,
    поэтому каждый запрос попадал в свой счётчик и защита от брутфорса пароля
    обходилась одним заголовком — лимит не срабатывал никогда.
    """
    limit = 3
    real_client_ip = '203.0.113.7'
    monkeypatch.setattr(settings, 'RATE_LIMIT_ENABLED', True)
    monkeypatch.setattr(settings, 'RATE_LIMIT_TIMES', limit)
    monkeypatch.setattr(settings, 'RATE_LIMIT_SECONDS', 60)

    def spoofed(attempt: int) -> dict[str, str]:
        return {'X-Forwarded-For': f'10.9.9.{attempt}, {real_client_ip}'}

    for attempt in range(limit):
        response = await client.post('/api/v1/auth/login', json=_LOGIN_PAYLOAD, headers=spoofed(attempt))
        assert response.status_code != 429

    response = await client.post('/api/v1/auth/login', json=_LOGIN_PAYLOAD, headers=spoofed(limit))
    assert response.status_code == 429


@pytest.mark.asyncio
async def test_headers_ignored_when_peer_is_not_a_trusted_proxy(client: AsyncClient, monkeypatch):
    """От недоверенного адреса заголовки с IP игнорируются целиком.

    Клиент, обратившийся к сервису напрямую (минуя Nginx), не должен управлять
    собственным ключом лимита — иначе он снова получает новый счётчик на запрос.
    """
    limit = 3
    monkeypatch.setattr(settings, 'RATE_LIMIT_ENABLED', True)
    monkeypatch.setattr(settings, 'RATE_LIMIT_TIMES', limit)
    monkeypatch.setattr(settings, 'RATE_LIMIT_SECONDS', 60)
    # ASGITransport подставляет адрес соединения 127.0.0.1 — исключаем его из
    # доверенных сетей, чтобы запрос выглядел пришедшим напрямую.
    monkeypatch.setattr(settings, 'AUTH_TRUSTED_PROXIES', '10.0.0.0/8')

    for attempt in range(limit):
        headers = {'X-Real-IP': f'198.51.100.{attempt}', 'X-Forwarded-For': f'198.51.100.{attempt}'}
        response = await client.post('/api/v1/auth/login', json=_LOGIN_PAYLOAD, headers=headers)
        assert response.status_code != 429

    response = await client.post(
        '/api/v1/auth/login',
        json=_LOGIN_PAYLOAD,
        headers={'X-Real-IP': '198.51.100.250', 'X-Forwarded-For': '198.51.100.250'},
    )
    assert response.status_code == 429
