import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_login_history_pagination(client: AsyncClient):
    """Тест пагинации истории входов."""
    # 1. Регистрация и логин пользователя
    user_data = {'login': 'pag_user', 'email': 'pag@test.com', 'password': 'Password123!'}
    await client.post('/api/v1/auth/register', json=user_data)

    login_resp = await client.post('/api/v1/auth/login', json={'login': 'pag_user', 'password': 'Password123!'})
    access_token = login_resp.json()['access_token']
    headers = {'Authorization': f'Bearer {access_token}'}

    # 2. Создаем несколько записей в истории (путем повторных логинов)
    # Первый логин уже создал одну запись. Добавим еще 5.
    for _ in range(5):
        await client.post('/api/v1/auth/login', json={'login': 'pag_user', 'password': 'Password123!'})

    # Итого в базе 6 записей для этого пользователя.

    # 3. Запрос первой страницы с размером 2
    response = await client.get('/api/v1/auth/login-history?page_number=1&page_size=2', headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2

    # 4. Запрос второй страницы с размером 2
    response = await client.get('/api/v1/auth/login-history?page_number=2&page_size=2', headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2

    # 5. Запрос четвертой страницы с размером 2 (должно быть пусто)
    response = await client.get('/api/v1/auth/login-history?page_number=4&page_size=2', headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 0

    # 6. Проверка значений по умолчанию (page_size=50)
    response = await client.get('/api/v1/auth/login-history', headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 6
