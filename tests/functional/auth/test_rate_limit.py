import pytest
from httpx import AsyncClient

from core.config import settings

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
