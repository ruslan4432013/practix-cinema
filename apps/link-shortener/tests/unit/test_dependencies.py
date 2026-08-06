"""Пропуск к межсервисной ручке: неверный токен — это 401, а не падение.

``secrets.compare_digest`` на СТРОКАХ требует, чтобы обе были из ASCII. Starlette
декодирует заголовки как latin-1, поэтому присланная кириллица давала здесь
``TypeError`` — то есть 500 с трейсбеком вместо честного 401. Ровно эта же
ловушка уже разобрана в сервисе нотификаций
(``practix_notifications/api/v1/views.py``), где сравниваются байты.
"""

import asyncio

import pytest
from fastapi import HTTPException

from practix_link_shortener.api.v1.dependencies import require_internal_token
from practix_link_shortener.core.config import settings


def _check(header: str) -> None:
    asyncio.run(require_internal_token(header))


def test_correct_token_passes():
    _check(f'Bearer {settings.SHORTENER_INTERNAL_TOKEN}')


@pytest.mark.parametrize(
    'header',
    [
        '',
        'Bearer',
        'Bearer wrong-token',
        'Basic sometoken',
        pytest.param('Bearer пароль', id='non-ascii'),
        pytest.param('Bearer 🔑', id='emoji'),
    ],
)
def test_bad_tokens_are_401_and_never_a_crash(header):
    with pytest.raises(HTTPException) as exc:
        _check(header)

    assert exc.value.status_code == 401


def test_prefix_is_required_even_for_the_right_secret():
    """Токен без схемы — это не токен: заголовок разбирается, а не ищется подстрокой."""
    with pytest.raises(HTTPException):
        _check(settings.SHORTENER_INTERNAL_TOKEN)
