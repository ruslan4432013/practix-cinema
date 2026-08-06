"""Защита от открытого редиректа.

Самый ценный набор в сервисе: пропущенная проверка здесь превращает наш домен в
инструмент фишинга — в адресной строке жертвы стоит `localhost`, а уводит он
куда угодно.
"""

import pytest

from practix_link_shortener.services.exceptions import InvalidTargetUrl
from practix_link_shortener.services.targets import validate_target

ALLOWED = frozenset({'localhost', '127.0.0.1'})


def check(url: str) -> str:
    return validate_target(url, allowed_hosts=ALLOWED, self_host='localhost', self_path_prefix='/s/')


@pytest.mark.parametrize(
    'url',
    [
        'http://localhost/',
        'https://localhost/catalog',
        'http://127.0.0.1:8080/film/42?utm=welcome',
    ],
)
def test_allowed_targets_pass(url):
    assert check(url) == url


def test_empty_target_is_rejected():
    with pytest.raises(InvalidTargetUrl):
        check('   ')


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'data:text/html,<h1>hi', 'file:///etc/passwd'])
def test_non_http_schemes_are_rejected(url):
    with pytest.raises(InvalidTargetUrl):
        check(url)


def test_protocol_relative_url_is_rejected():
    """`//evil.com` — валидный URL: браузер подставит текущую схему и уйдёт."""
    with pytest.raises(InvalidTargetUrl):
        check('//evil.com/phish')


def test_userinfo_trick_is_rejected():
    """`http://localhost@evil.com` ведёт на evil.com, а «начинается с localhost»."""
    with pytest.raises(InvalidTargetUrl):
        check('http://localhost@evil.com/phish')


def test_foreign_host_is_rejected():
    with pytest.raises(InvalidTargetUrl):
        check('https://evil.com/phish')


def test_self_referencing_short_link_is_rejected():
    """Короткая ссылка на короткую ссылку зациклила бы редирект."""
    with pytest.raises(InvalidTargetUrl):
        check('http://localhost/s/AbC1234')


def test_own_host_outside_short_prefix_is_allowed():
    """Уводить на свою же главную — штатный случай, и его ловить не надо."""
    assert check('http://localhost/catalog') == 'http://localhost/catalog'


def test_host_matching_is_case_insensitive():
    assert check('http://LOCALHOST/') == 'http://LOCALHOST/'
