import pytest
from httpx import AsyncClient
from sqlalchemy import text


async def _register_and_login(client, login, email, password='Password123!'):
    await client.post(
        '/api/v1/auth/register',
        json={'login': login, 'email': email, 'password': password},
    )
    resp = await client.post(
        '/api/v1/auth/login',
        json={'login': login, 'password': password},
    )
    return resp.json()['access_token']


async def _make_admin(login):
    from conftest import TestingSessionLocal

    async with TestingSessionLocal() as session:
        await session.execute(
            text('INSERT INTO roles (id, name) VALUES (:id, :name) ON CONFLICT DO NOTHING'),
            {'id': '00000000-0000-0000-0000-0000000000aa', 'name': 'admin'},
        )
        res = await session.execute(text('SELECT id FROM users WHERE login = :l'), {'l': login})
        user_id = res.scalar()
        await session.execute(
            text('INSERT INTO user_roles (user_id, role_id) VALUES (:u, :r) ON CONFLICT DO NOTHING'),
            {'u': user_id, 'r': '00000000-0000-0000-0000-0000000000aa'},
        )
        await session.commit()


@pytest.mark.asyncio
async def test_get_me(client: AsyncClient):
    token = await _register_and_login(client, 'me_user', 'me@example.com')
    resp = await client.get(
        '/api/v1/users/me',
        headers={'Authorization': f'Bearer {token}'},
    )
    assert resp.status_code == 200
    assert resp.json()['login'] == 'me_user'
    assert resp.json()['email'] == 'me@example.com'


@pytest.mark.asyncio
async def test_get_me_unauthorized(client: AsyncClient):
    resp = await client.get('/api/v1/users/me')
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_change_credentials_email(client: AsyncClient):
    token = await _register_and_login(client, 'cred_user', 'cred@example.com')
    resp = await client.patch(
        '/api/v1/users/me/credentials',
        json={'email': 'cred_new@example.com', 'current_password': 'Password123!'},
        headers={'Authorization': f'Bearer {token}'},
    )
    assert resp.status_code == 200
    assert resp.json()['email'] == 'cred_new@example.com'


@pytest.mark.asyncio
async def test_change_credentials_login(client: AsyncClient):
    token = await _register_and_login(client, 'login_user', 'login_u@example.com')
    resp = await client.patch(
        '/api/v1/users/me/credentials',
        json={'login': 'login_user_new', 'current_password': 'Password123!'},
        headers={'Authorization': f'Bearer {token}'},
    )
    assert resp.status_code == 200
    assert resp.json()['login'] == 'login_user_new'


@pytest.mark.asyncio
async def test_change_credentials_wrong_password(client: AsyncClient):
    token = await _register_and_login(client, 'wrongpw_user', 'wpw@example.com')
    resp = await client.patch(
        '/api/v1/users/me/credentials',
        json={'email': 'wpw_new@example.com', 'current_password': 'WrongPassword!'},
        headers={'Authorization': f'Bearer {token}'},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_change_credentials_duplicate(client: AsyncClient):
    await client.post(
        '/api/v1/auth/register',
        json={'login': 'occupied', 'email': 'occupied@example.com', 'password': 'Password123!'},
    )
    token = await _register_and_login(client, 'dup_user', 'dup@example.com')
    resp = await client.patch(
        '/api/v1/users/me/credentials',
        json={'email': 'occupied@example.com', 'current_password': 'Password123!'},
        headers={'Authorization': f'Bearer {token}'},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_check_permissions_valid_no_roles(client: AsyncClient):
    token = await _register_and_login(client, 'perm_user', 'perm@example.com')
    resp = await client.post(
        '/api/v1/users/check-permissions',
        json={'access_token': token, 'required_roles': []},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data['allowed'] is True
    assert data['is_superuser'] is False


@pytest.mark.asyncio
async def test_check_permissions_missing_role(client: AsyncClient):
    token = await _register_and_login(client, 'perm_no_role', 'pnr@example.com')
    resp = await client.post(
        '/api/v1/users/check-permissions',
        json={'access_token': token, 'required_roles': ['editor']},
    )
    assert resp.status_code == 200
    assert resp.json()['allowed'] is False


@pytest.mark.asyncio
async def test_check_permissions_invalid_token(client: AsyncClient):
    resp = await client.post(
        '/api/v1/users/check-permissions',
        json={'access_token': 'not-a-jwt', 'required_roles': []},
    )
    assert resp.status_code == 200
    assert resp.json()['allowed'] is False


@pytest.mark.asyncio
async def test_superuser_bypass_in_role_required(client: AsyncClient):
    # Регистрируем пользователя и делаем его админом
    await client.post(
        '/api/v1/auth/register',
        json={'login': 'bypass_admin', 'email': 'bp@example.com', 'password': 'Password123!'},
    )
    await _make_admin('bypass_admin')
    login_resp = await client.post(
        '/api/v1/auth/login',
        json={'login': 'bypass_admin', 'password': 'Password123!'},
    )
    token = login_resp.json()['access_token']

    # У admin есть доступ к ручкам ролей
    resp = await client.get(
        '/api/v1/roles/',
        headers={'Authorization': f'Bearer {token}'},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_check_permissions_superuser_flag(client: AsyncClient):
    await client.post(
        '/api/v1/auth/register',
        json={'login': 'su_check', 'email': 'suc@example.com', 'password': 'Password123!'},
    )
    await _make_admin('su_check')
    login_resp = await client.post(
        '/api/v1/auth/login',
        json={'login': 'su_check', 'password': 'Password123!'},
    )
    token = login_resp.json()['access_token']

    resp = await client.post(
        '/api/v1/users/check-permissions',
        json={'access_token': token, 'required_roles': ['editor']},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data['is_superuser'] is True
    assert data['allowed'] is True
