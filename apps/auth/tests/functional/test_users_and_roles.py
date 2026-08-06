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


@pytest.mark.asyncio
async def test_list_users_requires_admin(client: AsyncClient):
    """Выгрузка пользователей — межсервисная ручка, а не публичная."""
    token = await _register_and_login(client, 'plain_user', 'plain@example.com')
    resp = await client.get('/api/v1/users', headers={'Authorization': f'Bearer {token}'})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_list_users_unauthorized(client: AsyncClient):
    assert (await client.get('/api/v1/users')).status_code == 401


@pytest.mark.asyncio
async def test_list_users_returns_page_with_total(client: AsyncClient):
    """Нужна сервису нотификаций: он держит свою витрину контактов и обновляет
    её синхронизацией, чтобы рассылка не ходила в Auth за каждым получателем."""
    await _register_and_login(client, 'svc_admin', 'svc_admin@example.com')
    await _make_admin('svc_admin')
    token = await _register_and_login(client, 'svc_admin', 'svc_admin@example.com')
    await _register_and_login(client, 'reader_one', 'one@example.com')
    await _register_and_login(client, 'reader_two', 'two@example.com')

    resp = await client.get(
        '/api/v1/users',
        params={'limit': 2, 'offset': 0},
        headers={'Authorization': f'Bearer {token}'},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body['total'] == 3
    assert body['limit'] == 2
    assert body['offset'] == 0
    assert len(body['items']) == 2
    # Витрине нужны именно эти поля: id для upsert, email — адрес доставки.
    assert {'id', 'login', 'email', 'created_at'} <= set(body['items'][0])


@pytest.mark.asyncio
async def test_list_users_pagination_does_not_repeat_rows(client: AsyncClient):
    """Сортировка стабильная: иначе соседние страницы вернули бы одну и ту же
    запись и пропустили другую, а синхронизация тихо потеряла бы получателя."""
    await _register_and_login(client, 'page_admin', 'page_admin@example.com')
    await _make_admin('page_admin')
    token = await _register_and_login(client, 'page_admin', 'page_admin@example.com')
    for index in range(4):
        await _register_and_login(client, f'paged_{index}', f'paged_{index}@example.com')

    seen = []
    for offset in (0, 2, 4):
        resp = await client.get(
            '/api/v1/users',
            params={'limit': 2, 'offset': offset},
            headers={'Authorization': f'Bearer {token}'},
        )
        seen.extend(item['id'] for item in resp.json()['items'])

    assert len(seen) == len(set(seen)) == 5


# ---------------------------------------------------------------------------
# Имя, фамилия и пакетная выборка — то, чем воркер рассылки персонифицирует
# письмо. Он получает из очереди только идентификатор и приходит за остальным
# сюда.
#
# ВАЖНО: этот набор работает на `Base.metadata.create_all`, то есть саму
# alembic-миграцию он не проверяет. Её единственная проверка — контейнер
# auth-migrations в стенде нотификаций.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_accepts_and_returns_names(client: AsyncClient):
    resp = await client.post(
        '/api/v1/auth/register',
        json={
            'login': 'named_user',
            'email': 'named@example.com',
            'password': 'Password123!',
            'first_name': 'Иван',
            'last_name': 'Петров',
        },
    )

    assert resp.status_code in (200, 201), resp.text
    assert resp.json()['first_name'] == 'Иван'
    assert resp.json()['last_name'] == 'Петров'


@pytest.mark.asyncio
async def test_registration_without_names_leaves_them_null(client: AsyncClient):
    """У заведённых до этой версии имени нет физически, и NULL это показывает."""
    token = await _register_and_login(client, 'anon_user', 'anon@example.com')

    resp = await client.get('/api/v1/users/me', headers={'Authorization': f'Bearer {token}'})

    assert resp.json()['first_name'] is None
    assert resp.json()['last_name'] is None


@pytest.mark.asyncio
async def test_profile_update_changes_only_what_was_sent(client: AsyncClient):
    """PATCH обязан уметь менять одно поле, не зная про второе."""
    token = await _register_and_login(client, 'profile_user', 'profile@example.com')
    await client.patch(
        '/api/v1/users/me/profile',
        json={'first_name': 'Иван', 'last_name': 'Петров'},
        headers={'Authorization': f'Bearer {token}'},
    )

    resp = await client.patch(
        '/api/v1/users/me/profile',
        json={'first_name': 'Иоганн'},
        headers={'Authorization': f'Bearer {token}'},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()['first_name'] == 'Иоганн'
    assert resp.json()['last_name'] == 'Петров'


@pytest.mark.asyncio
async def test_profile_update_requires_authentication(client: AsyncClient):
    resp = await client.patch('/api/v1/users/me/profile', json={'first_name': 'Кто-то'})

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_lookup_returns_found_users_and_names_the_missing(client: AsyncClient):
    """`missing` перечисляется явно: «пользователь удалён» и «не тот id» —
    разные факты, и на первом строится пропуск `unknown_user` в журнале."""
    await _register_and_login(client, 'lookup_admin', 'lookup_admin@example.com')
    await _make_admin('lookup_admin')
    token = await _register_and_login(client, 'lookup_admin', 'lookup_admin@example.com')
    await client.post(
        '/api/v1/auth/register',
        json={
            'login': 'lookup_target',
            'email': 'target@example.com',
            'password': 'Password123!',
            'first_name': 'Анна',
            'last_name': 'Кузнецова',
        },
    )
    listed = await client.get('/api/v1/users', headers={'Authorization': f'Bearer {token}'})
    target = next(item for item in listed.json()['items'] if item['login'] == 'lookup_target')
    ghost = '00000000-0000-0000-0000-0000000000ff'

    resp = await client.post(
        '/api/v1/users/lookup',
        json={'ids': [target['id'], ghost]},
        headers={'Authorization': f'Bearer {token}'},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [item['id'] for item in body['items']] == [target['id']]
    assert body['items'][0]['first_name'] == 'Анна'
    assert body['items'][0]['email'] == 'target@example.com'
    assert body['missing'] == [ghost]


@pytest.mark.asyncio
async def test_lookup_is_admin_only(client: AsyncClient):
    """Личные данные всех пользователей — не то, что отдают любому владельцу токена."""
    token = await _register_and_login(client, 'plain_user', 'plain@example.com')

    resp = await client.post(
        '/api/v1/users/lookup',
        json={'ids': ['00000000-0000-0000-0000-0000000000ff']},
        headers={'Authorization': f'Bearer {token}'},
    )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_lookup_requires_authentication(client: AsyncClient):
    resp = await client.post('/api/v1/users/lookup', json={'ids': ['00000000-0000-0000-0000-0000000000ff']})

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_lookup_rejects_an_oversized_batch(client: AsyncClient):
    """Потолок в 500 — то, на что рассчитан NOTIFY_BUILDER_LOOKUP_BATCH."""
    await _register_and_login(client, 'big_admin', 'big_admin@example.com')
    await _make_admin('big_admin')
    token = await _register_and_login(client, 'big_admin', 'big_admin@example.com')

    resp = await client.post(
        '/api/v1/users/lookup',
        json={'ids': [f'00000000-0000-0000-0000-{index:012d}' for index in range(501)]},
        headers={'Authorization': f'Bearer {token}'},
    )

    assert resp.status_code == 422


async def _grant_role(login: str, role: str, role_id: str) -> None:
    """Выдать пользователю роль напрямую в базе — как это делает CLI."""
    from conftest import TestingSessionLocal

    async with TestingSessionLocal() as session:
        await session.execute(
            text('INSERT INTO roles (id, name) VALUES (:id, :name) ON CONFLICT DO NOTHING'),
            {'id': role_id, 'name': role},
        )
        res = await session.execute(text('SELECT id FROM users WHERE login = :l'), {'l': login})
        user_id = res.scalar()
        await session.execute(
            text('INSERT INTO user_roles (user_id, role_id) VALUES (:u, :r) ON CONFLICT DO NOTHING'),
            {'u': user_id, 'r': role_id},
        )
        await session.commit()
    return user_id


CONFIRMER_ROLE_ID = '00000000-0000-0000-0000-0000000000cc'


async def _confirmer_token(client) -> str:
    """Токен служебной учётки шортенера — с УЗКОЙ ролью, не с admin."""
    await _register_and_login(client, 'svc_shortener', 'svc_shortener@example.com')
    await _grant_role('svc_shortener', 'email-confirmer', CONFIRMER_ROLE_ID)
    return await _register_and_login(client, 'svc_shortener', 'svc_shortener@example.com')


@pytest.mark.asyncio
async def test_registration_leaves_the_email_unconfirmed(client: AsyncClient):
    """Ввести адрес — не то же самое, что им владеть."""
    token = await _register_and_login(client, 'fresh_user', 'fresh@example.com')

    resp = await client.get('/api/v1/users/me', headers={'Authorization': f'Bearer {token}'})

    assert resp.status_code == 200
    assert resp.json()['email_verified'] is False


@pytest.mark.asyncio
async def test_confirm_email_sets_the_flag(client: AsyncClient):
    user_token = await _register_and_login(client, 'confirm_me', 'confirm_me@example.com')
    user_id = (await client.get('/api/v1/users/me', headers={'Authorization': f'Bearer {user_token}'})).json()['id']
    token = await _confirmer_token(client)

    resp = await client.post(
        f'/api/v1/users/{user_id}/confirm-email',
        headers={'Authorization': f'Bearer {token}'},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {'status': 'confirmed', 'user_id': user_id}
    me = await client.get('/api/v1/users/me', headers={'Authorization': f'Bearer {user_token}'})
    assert me.json()['email_verified'] is True


@pytest.mark.asyncio
async def test_confirm_email_is_idempotent(client: AsyncClient):
    """Повтор — это 200 already_confirmed, а не 409.

    По ссылке из письма кликают дважды, а до человека по ней ходит сканер
    почтового клиента. У вызывающего должен быть ОДИН путь успеха.
    """
    user_token = await _register_and_login(client, 'twice_user', 'twice@example.com')
    user_id = (await client.get('/api/v1/users/me', headers={'Authorization': f'Bearer {user_token}'})).json()['id']
    token = await _confirmer_token(client)
    headers = {'Authorization': f'Bearer {token}'}

    first = await client.post(f'/api/v1/users/{user_id}/confirm-email', headers=headers)
    second = await client.post(f'/api/v1/users/{user_id}/confirm-email', headers=headers)

    assert first.json()['status'] == 'confirmed'
    assert second.status_code == 200
    assert second.json()['status'] == 'already_confirmed'


@pytest.mark.asyncio
async def test_confirm_email_of_an_unknown_user_is_404(client: AsyncClient):
    token = await _confirmer_token(client)

    resp = await client.post(
        '/api/v1/users/00000000-0000-0000-0000-0000000000ff/confirm-email',
        headers={'Authorization': f'Bearer {token}'},
    )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_confirm_email_requires_the_narrow_role(client: AsyncClient):
    """Обычный владелец токена не должен уметь подтверждать чужие адреса."""
    user_token = await _register_and_login(client, 'stranger', 'stranger@example.com')
    user_id = (await client.get('/api/v1/users/me', headers={'Authorization': f'Bearer {user_token}'})).json()['id']

    resp = await client.post(
        f'/api/v1/users/{user_id}/confirm-email',
        headers={'Authorization': f'Bearer {user_token}'},
    )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_confirm_email_requires_authentication(client: AsyncClient):
    resp = await client.post('/api/v1/users/00000000-0000-0000-0000-0000000000ff/confirm-email')

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_changing_the_email_drops_the_confirmation(client: AsyncClient):
    """Подтверждён был СТАРЫЙ адрес.

    Оставить флаг — значит объявить подтверждённым ящик, который человек,
    возможно, не открывал ни разу.
    """
    user_token = await _register_and_login(client, 'mover', 'mover@example.com')
    user_id = (await client.get('/api/v1/users/me', headers={'Authorization': f'Bearer {user_token}'})).json()['id']
    token = await _confirmer_token(client)
    await client.post(f'/api/v1/users/{user_id}/confirm-email', headers={'Authorization': f'Bearer {token}'})

    resp = await client.patch(
        '/api/v1/users/me/credentials',
        json={'email': 'mover_new@example.com', 'current_password': 'Password123!'},
        headers={'Authorization': f'Bearer {user_token}'},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()['email_verified'] is False
