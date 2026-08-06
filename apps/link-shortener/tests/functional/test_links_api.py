"""Ручка создания ссылок."""

import uuid

from sqlalchemy import text

from practix_link_shortener.core.config import settings

PATH = '/api/v1/links'


async def test_creating_a_link_returns_201_and_a_short_url(client, internal_headers):
    response = await client.post(PATH, json={'target_url': 'http://localhost/'}, headers=internal_headers)

    assert response.status_code == 201
    body = response.json()
    assert len(body['code']) == settings.SHORTENER_CODE_LENGTH
    assert body['short_url'] == f'{settings.redirect_prefix}/{body["code"]}'
    assert body['kind'] == 'redirect'


async def test_same_idempotency_key_returns_200_and_the_same_link(client, internal_headers):
    payload = {'target_url': 'http://localhost/', 'idempotency_key': 'batch-1'}

    first = await client.post(PATH, json=payload, headers=internal_headers)
    second = await client.post(PATH, json=payload, headers=internal_headers)

    assert first.status_code == 201
    # 200, а не 201: ссылка не создана, а найдена. Пересобранная пачка писем не
    # должна выпускать человеку вторую ссылку.
    assert second.status_code == 200
    assert first.json()['code'] == second.json()['code']


async def test_confirm_link_requires_a_user(client, internal_headers):
    response = await client.post(
        PATH, json={'target_url': 'http://localhost/', 'kind': 'confirm_email'}, headers=internal_headers
    )
    assert response.status_code == 400


async def test_foreign_host_is_refused(client, internal_headers):
    """Открытый редирект отсекается на записи: к переходу ссылка уже в письме."""
    response = await client.post(PATH, json={'target_url': 'https://evil.com/'}, headers=internal_headers)
    assert response.status_code == 400


async def test_creation_requires_the_internal_token(client):
    response = await client.post(PATH, json={'target_url': 'http://localhost/'})
    assert response.status_code == 401


async def test_wrong_internal_token_is_refused(client):
    response = await client.post(
        PATH, json={'target_url': 'http://localhost/'}, headers={'Authorization': 'Bearer nope'}
    )
    assert response.status_code == 401


async def test_unknown_field_is_refused(client, internal_headers):
    """``extra='forbid'``: опечатка вызывающего должна падать, а не молчать."""
    response = await client.post(
        PATH, json={'target_url': 'http://localhost/', 'ttl_hourz': 5}, headers=internal_headers
    )
    assert response.status_code == 422


async def test_ttl_is_capped_by_the_maximum(client, internal_headers):
    response = await client.post(
        PATH,
        json={'target_url': 'http://localhost/', 'ttl_hours': settings.SHORTENER_MAX_TTL_HOURS * 10},
        headers=internal_headers,
    )
    assert response.status_code == 201


async def test_introspection_shows_the_three_required_parts(client, internal_headers):
    """Задание требует, чтобы ссылка «включала» id, срок и redirectUrl.

    Код — непрозрачный ключ к ним, и вот они, одним запросом.
    """
    user_id = str(uuid.uuid4())
    created = await client.post(
        PATH,
        json={'target_url': 'http://localhost/', 'kind': 'confirm_email', 'user_id': user_id},
        headers=internal_headers,
    )
    code = created.json()['code']

    info = await client.get(f'{PATH}/{code}', headers=internal_headers)

    assert info.status_code == 200
    body = info.json()
    assert body['user_id'] == user_id
    assert body['target_url'] == 'http://localhost/'
    assert body['expires_at']
    assert body['visit_count'] == 0


async def test_introspection_of_unknown_code_is_404(client, internal_headers):
    response = await client.get(f'{PATH}/zzzzzzz', headers=internal_headers)
    assert response.status_code == 404


async def test_idempotent_replay_revives_an_expired_link(client, internal_headers, db):
    """Повтор с тем же ключом не имеет права вернуть мёртвую ссылку.

    Ключ идемпотентности здесь — ``DeliveryTask.idempotency_key``, то есть
    переотправка рассылки приходит с ним спустя дни. Возвращая строку как есть,
    сервис вкладывал бы в новое письмо ссылку, которая уже отвечает страницей
    404. Код при этом обязан сохраниться: ранее разосланные письма должны
    продолжать работать, а не расщепиться на два адреса.
    """
    payload = {'target_url': 'http://localhost/', 'idempotency_key': 'campaign-42'}
    first = await client.post(PATH, json=payload, headers=internal_headers)
    code = first.json()['code']

    await db.execute(text("UPDATE short_link SET expires_at = now() - interval '1 day' WHERE code = :c"), {'c': code})
    await db.commit()
    assert (await client.get(f'/s/{code}')).status_code == 404

    second = await client.post(PATH, json=payload, headers=internal_headers)

    assert second.status_code == 200
    assert second.json()['code'] == code
    assert (await client.get(f'/s/{code}')).status_code == 302


async def test_idempotent_replay_does_not_extend_a_live_link(client, internal_headers, db):
    """Живую ссылку повтор не трогает: иначе TTL стал бы бесконечным."""
    payload = {'target_url': 'http://localhost/', 'idempotency_key': 'campaign-43'}
    first = await client.post(PATH, json=payload, headers=internal_headers)
    code = first.json()['code']
    before = (await db.execute(text('SELECT expires_at FROM short_link WHERE code = :c'), {'c': code})).scalar_one()

    await client.post(PATH, json=payload, headers=internal_headers)

    after = (await db.execute(text('SELECT expires_at FROM short_link WHERE code = :c'), {'c': code})).scalar_one()
    assert after == before


async def test_idempotent_replay_revives_a_revoked_link(client, internal_headers, db):
    """Повтор с тем же ключом — это решение выслать рассылку снова."""
    payload = {'target_url': 'http://localhost/', 'idempotency_key': 'campaign-44'}
    code = (await client.post(PATH, json=payload, headers=internal_headers)).json()['code']
    await db.execute(text('UPDATE short_link SET revoked_at = now() WHERE code = :c'), {'c': code})
    await db.commit()

    second = await client.post(PATH, json=payload, headers=internal_headers)

    assert second.json()['code'] == code
    assert (await client.get(f'/s/{code}')).status_code == 302
