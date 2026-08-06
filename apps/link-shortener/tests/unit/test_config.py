"""Конфигурация: белый список окружений и запрет небезопасных дефолтов в проде."""

import pytest

from practix_link_shortener.core.config import (
    INSECURE_DEFAULT_AUTH_PASSWORD,
    INSECURE_DEFAULT_INTERNAL_TOKEN,
    Settings,
)

SAFE = {
    'SHORTENER_INTERNAL_TOKEN': 'real-token',
    'SHORTENER_AUTH_SERVICE_PASSWORD': 'real-password',
}


def test_unknown_environment_is_rejected():
    with pytest.raises(ValueError, match='SHORTENER_ENV'):
        Settings(SHORTENER_ENV='staging', **SAFE)


def test_prod_refuses_default_internal_token():
    with pytest.raises(ValueError, match='SHORTENER_INTERNAL_TOKEN'):
        Settings(
            SHORTENER_ENV='prod',
            SHORTENER_INTERNAL_TOKEN=INSECURE_DEFAULT_INTERNAL_TOKEN,
            SHORTENER_AUTH_SERVICE_PASSWORD='real-password',
        )


def test_prod_refuses_default_service_password():
    with pytest.raises(ValueError, match='SHORTENER_AUTH_SERVICE_PASSWORD'):
        Settings(
            SHORTENER_ENV='prod',
            SHORTENER_INTERNAL_TOKEN='real-token',
            SHORTENER_AUTH_SERVICE_PASSWORD=INSECURE_DEFAULT_AUTH_PASSWORD,
        )


def test_dev_tolerates_defaults_but_reports_them():
    """В dev дефолты работают — и именно поэтому их перечисляют в лог при старте."""
    settings = Settings(
        SHORTENER_ENV='dev',
        SHORTENER_INTERNAL_TOKEN=INSECURE_DEFAULT_INTERNAL_TOKEN,
        SHORTENER_AUTH_SERVICE_PASSWORD=INSECURE_DEFAULT_AUTH_PASSWORD,
    )
    assert len(settings.insecure_defaults) == 2


def test_allowed_hosts_are_parsed_and_normalised():
    settings = Settings(SHORTENER_ALLOWED_REDIRECT_HOSTS=' Localhost , 127.0.0.1 ,', **SAFE)
    assert settings.allowed_redirect_hosts == frozenset({'localhost', '127.0.0.1'})


def test_redirect_prefix_has_no_double_slash():
    settings = Settings(SHORTENER_PUBLIC_BASE_URL='http://localhost/', SHORTENER_REDIRECT_PATH='/s/', **SAFE)
    assert settings.redirect_prefix == 'http://localhost/s'


def test_public_host_is_extracted_from_base_url():
    settings = Settings(SHORTENER_PUBLIC_BASE_URL='http://Cinema.Example:8080', **SAFE)
    assert settings.public_host == 'cinema.example'
