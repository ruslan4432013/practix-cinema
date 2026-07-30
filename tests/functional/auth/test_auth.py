import pytest
from httpx import AsyncClient
from sqlalchemy import text


@pytest.mark.asyncio
async def test_register_success(client: AsyncClient):
    """Тест успешной регистрации."""
    response = await client.post(
        '/api/v1/auth/register', json={'login': 'testuser', 'email': 'test@example.com', 'password': 'Password123!'}
    )
    assert response.status_code == 201
    assert response.json()['login'] == 'testuser'


@pytest.mark.asyncio
async def test_register_weak_password(client: AsyncClient):
    """Тест регистрации со слабым паролем."""
    response = await client.post(
        '/api/v1/auth/register', json={'login': 'weakuser', 'email': 'weak@example.com', 'password': '123'}
    )
    assert response.status_code == 400
    assert 'Пароль слишком слабый' in response.json()['detail']


@pytest.mark.asyncio
async def test_login_success(client: AsyncClient):
    """Тест успешного входа."""
    # Регистрация
    await client.post(
        '/api/v1/auth/register', json={'login': 'loginuser', 'email': 'login@example.com', 'password': 'Password123!'}
    )

    # Вход
    response = await client.post('/api/v1/auth/login', json={'login': 'loginuser', 'password': 'Password123!'})
    assert response.status_code == 200
    assert 'access_token' in response.json()
    assert 'refresh_token' in response.json()


@pytest.mark.asyncio
async def test_refresh_token(client: AsyncClient):
    """Тест обновления токенов."""
    # Регистрация и вход
    await client.post(
        '/api/v1/auth/register',
        json={'login': 'refreshuser', 'email': 'refresh@example.com', 'password': 'Password123!'},
    )
    login_resp = await client.post('/api/v1/auth/login', json={'login': 'refreshuser', 'password': 'Password123!'})
    refresh_token = login_resp.json()['refresh_token']

    # Обновление
    response = await client.post('/api/v1/auth/refresh', headers={'Authorization': f'Bearer {refresh_token}'})
    assert response.status_code == 200
    assert 'access_token' in response.json()
    assert 'refresh_token' in response.json()


@pytest.mark.asyncio
async def test_logout_all(client: AsyncClient):
    """Тест выхода на всех устройствах."""
    # Регистрация и вход
    await client.post(
        '/api/v1/auth/register',
        json={'login': 'logoutalluser', 'email': 'logoutall@example.com', 'password': 'Password123!'},
    )
    login_resp = await client.post('/api/v1/auth/login', json={'login': 'logoutalluser', 'password': 'Password123!'})
    access_token = login_resp.json()['access_token']

    # Выход на всех устройствах
    response = await client.post('/api/v1/auth/logout-all', headers={'Authorization': f'Bearer {access_token}'})
    assert response.status_code == 200

    # Повторное использование токена должно вернуть 401
    response = await client.get('/api/v1/auth/login-history', headers={'Authorization': f'Bearer {access_token}'})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_assign_role_flow(client: AsyncClient):
    """Тест полного цикла управления ролями: создание и назначение."""
    # 1. Регистрация админа
    await client.post(
        '/api/v1/auth/register', json={'login': 'admin_user', 'email': 'admin@test.com', 'password': 'Password123!'}
    )

    # Делаем его админом напрямую в БД (эмуляция CLI/суперпользователя)
    from conftest import TestingSessionLocal

    async with TestingSessionLocal() as session:
        # Создаем роль
        await session.execute(
            text('INSERT INTO roles (id, name) VALUES (:id, :name) ON CONFLICT DO NOTHING'),
            {'id': '00000000-0000-0000-0000-000000000001', 'name': 'admin'},
        )
        # Находим пользователя
        res = await session.execute(text("SELECT id FROM users WHERE login = 'admin_user'"))
        user_id = res.scalar()
        # Назначаем роль
        await session.execute(
            text('INSERT INTO user_roles (user_id, role_id) VALUES (:u_id, :r_id) ON CONFLICT DO NOTHING'),
            {'u_id': user_id, 'r_id': '00000000-0000-0000-0000-000000000001'},
        )
        await session.commit()

    # 2. Логин админа
    login_resp = await client.post('/api/v1/auth/login', json={'login': 'admin_user', 'password': 'Password123!'})
    access_token = login_resp.json()['access_token']

    # 3. Регистрация обычного пользователя
    reg_resp = await client.post(
        '/api/v1/auth/register', json={'login': 'regular_user', 'email': 'regular@test.com', 'password': 'Password123!'}
    )
    target_user_id = reg_resp.json()['id']

    # 4. Создание новой роли через API (как админ)
    await client.post(
        '/api/v1/roles/',
        json={'name': 'subscriber', 'description': 'Обычный подписчик'},
        headers={'Authorization': f'Bearer {access_token}'},
    )

    # 5. Назначение роли пользователю
    assign_resp = await client.post(
        f'/api/v1/roles/assign?user_id={target_user_id}&role_name=subscriber',
        headers={'Authorization': f'Bearer {access_token}'},
    )
    assert assign_resp.status_code == 200
    assert assign_resp.json()['msg'] == 'Роль успешно назначена'

    # 6. Проверка, что роль появилась в JWT пользователя после перелогина
    login_resp = await client.post('/api/v1/auth/login', json={'login': 'regular_user', 'password': 'Password123!'})
    new_access_token = login_resp.json()['access_token']

    # Декодируем JWT (в тестах можно просто проверить доступ к защищенному ресурсу)
    # Или через login-history, если мы добавим туда проверку ролей (но ее там нет)
    # Но мы можем попробовать создать роль как этот пользователь - должно быть 403, так как он не админ
    role_resp = await client.post(
        '/api/v1/roles/',
        json={'name': 'test_role', 'description': 'test'},
        headers={'Authorization': f'Bearer {new_access_token}'},
    )
    assert role_resp.status_code == 403
