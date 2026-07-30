from datetime import datetime

import pytest
from conftest import DATABASE_URL
from httpx import AsyncClient
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import create_async_engine

UA_WEB = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
)
UA_MOBILE = (
    'Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) '
    'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1'
)
UA_SMART = (
    'Mozilla/5.0 (SMART-TV; Linux; Tizen 6.0) AppleWebKit/537.36 '
    '(KHTML, like Gecko) SamsungBrowser/4.0 TV Safari/537.36'
)


@pytest.mark.asyncio
async def test_login_history_partitioned_by_device(client: AsyncClient):
    """Записи истории входов раскладываются по LIST-секциям устройства."""
    user = {'login': 'dev_user', 'email': 'dev@test.com', 'password': 'Password123!'}
    await client.post('/api/v1/auth/register', json=user)

    creds = {'login': 'dev_user', 'password': 'Password123!'}
    await client.post('/api/v1/auth/login', json=creds, headers={'user-agent': UA_WEB})
    login_resp = await client.post('/api/v1/auth/login', json=creds, headers={'user-agent': UA_MOBILE})
    await client.post('/api/v1/auth/login', json=creds, headers={'user-agent': UA_SMART})

    token = login_resp.json()['access_token']
    headers = {'Authorization': f'Bearer {token}'}

    # 1. device_type сохраняется и отдаётся в API.
    resp = await client.get('/api/v1/auth/login-history', headers=headers)
    assert resp.status_code == 200
    device_types = {row['device_type'] for row in resp.json()}
    assert device_types == {'web', 'mobile', 'smart'}

    # 2. Физически строки лежат в нужных секциях по устройству.
    year = datetime.utcnow().year
    engine = create_async_engine(DATABASE_URL, poolclass=pool.NullPool)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(text('SELECT device_type, tableoid::regclass::text AS partition FROM login_history'))
            ).all()
    finally:
        await engine.dispose()

    placement = {r.device_type: r.partition for r in rows}
    assert placement['web'] == f'login_history_{year}_web'
    assert placement['mobile'] == f'login_history_{year}_mobile'
    assert placement['smart'] == f'login_history_{year}_smart'
