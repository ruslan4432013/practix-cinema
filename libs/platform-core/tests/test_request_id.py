"""Проверки обеих политик ``RequestIdMiddleware``.

Смысл — зафиксировать, что извлечение НЕ слило два разных требования в одно:
``rest``/``auth`` обязаны отвечать 400 на отсутствующий заголовок, а коллектор
обязан его генерировать, иначе beacon-события браузера перестанут доезжать.
"""

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from practix_core.request_id import (
    REQUEST_ID_HEADER,
    RequestIdMiddleware,
    get_request_id,
    request_id_ctx,
)


def _app(**middleware_kwargs) -> Starlette:
    async def echo(_request):
        # Значение должно быть видно обработчику через ContextVar — именно так
        # его читают логгер, продюсер Kafka и исходящие межсервисные клиенты.
        return PlainTextResponse(get_request_id() or 'MISSING')

    app = Starlette(routes=[Route('/echo', echo)])
    app.add_middleware(RequestIdMiddleware, **middleware_kwargs)
    return app


# --- Политика rest/auth: отсутствие заголовка -> 400 ------------------------


def test_reject_400_is_the_default_policy():
    with TestClient(_app()) as client:
        response = client.get('/echo')
    assert response.status_code == 400
    assert REQUEST_ID_HEADER in response.json()['detail']


def test_reject_400_passes_through_supplied_id():
    with TestClient(_app()) as client:
        response = client.get('/echo', headers={REQUEST_ID_HEADER: 'abc-123'})
    assert response.status_code == 200
    assert response.text == 'abc-123'
    assert response.headers[REQUEST_ID_HEADER] == 'abc-123'


# --- Политика коллектора: отсутствие заголовка -> UUID ----------------------


def test_generate_policy_creates_id_when_header_absent():
    with TestClient(_app(on_missing='generate')) as client:
        response = client.get('/echo')
    assert response.status_code == 200
    assert response.text != 'MISSING'
    # Возвращённый заголовок совпадает с тем, что видел обработчик.
    assert response.headers[REQUEST_ID_HEADER] == response.text
    assert len(response.text) == 36  # str(uuid4())


def test_generate_policy_still_prefers_supplied_id():
    with TestClient(_app(on_missing='generate')) as client:
        response = client.get('/echo', headers={REQUEST_ID_HEADER: 'from-nginx'})
    assert response.text == 'from-nginx'


# --- Санитизация ------------------------------------------------------------


def test_sanitize_strips_control_characters():
    with TestClient(_app(on_missing='generate', sanitize=True)) as client:
        response = client.get('/echo', headers={REQUEST_ID_HEADER: 'ok\tid'})
    assert response.text == 'okid'


def test_sanitize_truncates_to_max_length():
    with TestClient(_app(on_missing='generate', sanitize=True, max_length=8)) as client:
        response = client.get('/echo', headers={REQUEST_ID_HEADER: 'x' * 100})
    assert response.text == 'x' * 8


def test_sanitize_falls_back_to_uuid_when_value_becomes_empty():
    """Заголовок только из управляющих символов не должен давать пустой id."""
    with TestClient(_app(on_missing='generate', sanitize=True)) as client:
        response = client.get('/echo', headers={REQUEST_ID_HEADER: '\t\n\x00'})
    assert len(response.text) == 36


def test_without_sanitize_value_is_left_alone():
    """rest/auth не санитизируют: за Nginx значение всегда своё."""
    with TestClient(_app()) as client:
        response = client.get('/echo', headers={REQUEST_ID_HEADER: 'a' * 200})
    assert response.text == 'a' * 200


# --- Гигиена контекста ------------------------------------------------------


def test_context_var_is_reset_after_response():
    with TestClient(_app()) as client:
        client.get('/echo', headers={REQUEST_ID_HEADER: 'transient'})
    assert request_id_ctx.get() is None


@pytest.mark.parametrize('policy', ['reject_400', 'generate'])
def test_custom_header_name_is_honoured(policy):
    with TestClient(_app(on_missing=policy, header='X-Corr-Id')) as client:
        response = client.get('/echo', headers={'X-Corr-Id': 'corr'})
    assert response.text == 'corr'
    assert response.headers['X-Corr-Id'] == 'corr'
