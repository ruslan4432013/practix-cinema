"""Публичный маршрут ``/s/{code}`` — единственный, который открывает человек."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from practix_link_shortener.services.exceptions import (
    ConfirmMisconfigured,
    ConfirmRejected,
    ConfirmUnavailable,
)

LINKS = '/api/v1/links'


async def make_link(client, headers, **payload) -> str:
    body = {'target_url': 'http://localhost/', **payload}
    response = await client.post(LINKS, json=body, headers=headers)
    assert response.status_code in (200, 201), response.text
    return response.json()['code']


async def visit_count(db, code: str) -> int:
    return await db.scalar(text('SELECT visit_count FROM short_link WHERE code = :c'), {'c': code})


async def test_valid_link_redirects_with_302_and_no_store(client, internal_headers):
    code = await make_link(client, internal_headers)

    response = await client.get(f'/s/{code}')

    # 302, а не 301: 301 кэшируется навсегда, и отозванная ссылка продолжала бы
    # уводить из кэша — прямое противоречие требованию «просроченная → 404».
    assert response.status_code == 302
    assert response.headers['location'] == 'http://localhost/'
    assert response.headers['cache-control'] == 'no-store'


async def test_plain_redirect_never_calls_auth(client, internal_headers, fake_auth):
    code = await make_link(client, internal_headers)
    await client.get(f'/s/{code}')
    assert fake_auth.calls == []


async def test_confirm_link_confirms_then_redirects(client, internal_headers, fake_auth):
    user_id = str(uuid.uuid4())
    code = await make_link(client, internal_headers, kind='confirm_email', user_id=user_id)

    response = await client.get(f'/s/{code}')

    assert fake_auth.calls == [user_id]
    assert response.status_code == 302
    # Порядок важен: редирект случается ПОСЛЕ подтверждения, как требует задание.
    assert response.headers['location'] == 'http://localhost/'


async def test_second_click_is_idempotent(client, internal_headers, fake_auth, db):
    """По ссылке кликают дважды, а до человека по ней ходит сканер почты."""
    code = await make_link(client, internal_headers, kind='confirm_email', user_id=str(uuid.uuid4()))

    await client.get(f'/s/{code}')
    fake_auth.status = 'already_confirmed'
    second = await client.get(f'/s/{code}')

    assert second.status_code == 302
    assert len(fake_auth.calls) == 2
    assert await visit_count(db, code) == 2


async def test_unknown_code_is_an_html_404(client):
    response = await client.get('/s/zzzzzzz')

    assert response.status_code == 404
    assert response.headers['content-type'].startswith('text/html')
    assert response.headers['cache-control'] == 'no-store'


async def test_expired_link_is_404(client, internal_headers, db):
    code = await make_link(client, internal_headers)
    await db.execute(
        text('UPDATE short_link SET expires_at = :t WHERE code = :c'),
        {'t': datetime.now(UTC) - timedelta(hours=1), 'c': code},
    )
    await db.commit()

    response = await client.get(f'/s/{code}')

    assert response.status_code == 404


async def test_revoked_link_is_404(client, internal_headers, db):
    """Отозванная, протухшая и несуществующая отвечают ОДИНАКОВО — намеренно.

    Разные ответы стали бы оракулом: перебирающий узнавал бы, какие коды есть.
    """
    code = await make_link(client, internal_headers)
    await db.execute(
        text('UPDATE short_link SET revoked_at = now() WHERE code = :c'),
        {'c': code},
    )
    await db.commit()

    response = await client.get(f'/s/{code}')

    assert response.status_code == 404


async def test_auth_outage_is_503_not_404(client, internal_headers, fake_auth):
    """Ссылка жива, не работает система. 404 сказал бы человеку «выбросьте её»."""
    code = await make_link(client, internal_headers, kind='confirm_email', user_id=str(uuid.uuid4()))
    fake_auth.raises = ConfirmUnavailable('Auth недоступен')

    response = await client.get(f'/s/{code}')

    assert response.status_code == 503
    assert response.headers['retry-after'] == '60'
    assert 'location' not in response.headers


async def test_misconfiguration_is_503_too(client, internal_headers, fake_auth):
    """Не заведена служебная учётка — проблема оператора, а не пользователя."""
    code = await make_link(client, internal_headers, kind='confirm_email', user_id=str(uuid.uuid4()))
    fake_auth.raises = ConfirmMisconfigured('нет роли')

    response = await client.get(f'/s/{code}')

    assert response.status_code == 503


async def test_deleted_user_is_404(client, internal_headers, fake_auth):
    code = await make_link(client, internal_headers, kind='confirm_email', user_id=str(uuid.uuid4()))
    fake_auth.raises = ConfirmRejected('нет пользователя')

    response = await client.get(f'/s/{code}')

    assert response.status_code == 404


@pytest.mark.parametrize('visits', [1, 2, 3])
async def test_visits_are_counted(client, internal_headers, db, visits):
    code = await make_link(client, internal_headers)

    for _ in range(visits):
        await client.get(f'/s/{code}')

    assert await visit_count(db, code) == visits


async def test_head_does_not_count_a_visit(client, internal_headers, db):
    """HEAD ходят сканеры ссылок почтовых клиентов, а не люди."""
    code = await make_link(client, internal_headers)

    response = await client.head(f'/s/{code}')

    assert response.status_code == 302
    assert await visit_count(db, code) == 0


async def test_head_does_not_confirm_the_address(client, internal_headers, db, fake_auth):
    """Сканер почтового клиента не имеет права подтвердить адрес за человека.

    HEAD уже не считался визитом — по той причине, что так ходят Safe Links и
    антивирусные шлюзы, а не люди. Но подтверждение по нему всё равно
    происходило, то есть смысл ссылки отдавался почтовому провайдеру: адрес
    оказывался «подтверждён» до того, как письмо вообще открыли.
    """
    code = await make_link(client, internal_headers, kind='confirm_email', user_id=str(uuid.uuid4()))

    response = await client.head(f'/s/{code}')

    assert response.status_code == 302, 'живость ссылки HEAD проверяет как обычно'
    assert fake_auth.calls == [], 'Auth не должен быть потревожен сканером'
    assert await visit_count(db, code) == 0


async def test_head_on_a_dead_link_is_still_404(client, internal_headers, db, fake_auth):
    """«Не подтверждать» не значит «отвечать 302 на что угодно»."""
    code = await make_link(client, internal_headers, kind='confirm_email', user_id=str(uuid.uuid4()))
    await db.execute(text('UPDATE short_link SET revoked_at = now() WHERE code = :c'), {'c': code})
    await db.commit()

    response = await client.head(f'/s/{code}')

    assert response.status_code == 404
    assert fake_auth.calls == []


async def test_a_human_click_after_a_scanner_still_confirms(client, internal_headers, fake_auth):
    """Проверка того, что HEAD ничего не сломал: человек за сканером доходит."""
    code = await make_link(client, internal_headers, kind='confirm_email', user_id=str(uuid.uuid4()))

    await client.head(f'/s/{code}')
    response = await client.get(f'/s/{code}')

    assert response.status_code == 302
    assert len(fake_auth.calls) == 1
