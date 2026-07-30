"""Функциональные тесты авторизации и доступа к контенту по подписке.

Проверяется:
- открытость просмотра каталога для анонимных пользователей (premium-only scope);
- требование токена для контента по подписке;
- ролевое разграничение (subscriber/admin);
- изящная деградация: в тестовом окружении Auth-сервис недоступен
  (host `auth` не резолвится), поэтому проверка прав для non-subscriber токена
  падает обратно на роли из самого токена, а сервис выдачи контента НЕ отдаёт 5xx.
"""

import uuid

import pytest

from practix_testing.utils.helpers import auth_header, make_film

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def public_and_premium_films(es_movies_index, es_write_data, flush_redis):
    public = make_film(title='Public Movie', access_type='public')
    premium = make_film(title='Premium Movie', access_type='subscribers')
    await es_write_data([public, premium], es_movies_index)
    return {'public': public, 'premium': premium}


class TestPublicContentOpen:
    """Просмотр каталога открыт для анонимных пользователей."""

    async def test_list_anonymous_ok(self, make_get_request, public_and_premium_films):
        response = await make_get_request('/api/v1/films')
        assert response.status == 200

    async def test_public_film_anonymous_ok(self, make_get_request, public_and_premium_films):
        film_id = public_and_premium_films['public']['id']
        response = await make_get_request(f'/api/v1/films/{film_id}')
        assert response.status == 200

    async def test_public_film_with_token_ok(self, make_get_request, public_and_premium_films):
        film_id = public_and_premium_films['public']['id']
        response = await make_get_request(f'/api/v1/films/{film_id}', headers=auth_header(roles=['user']))
        assert response.status == 200


class TestPremiumContentAccess:
    """Контент по подписке требует валидного токена и роли."""

    async def test_premium_anonymous_unauthorized(self, make_get_request, public_and_premium_films):
        film_id = public_and_premium_films['premium']['id']
        response = await make_get_request(f'/api/v1/films/{film_id}')
        assert response.status == 401

    async def test_premium_subscriber_ok(self, make_get_request, public_and_premium_films):
        film_id = public_and_premium_films['premium']['id']
        response = await make_get_request(f'/api/v1/films/{film_id}', headers=auth_header(roles=['subscriber']))
        assert response.status == 200

    async def test_premium_admin_ok(self, make_get_request, public_and_premium_films):
        film_id = public_and_premium_films['premium']['id']
        response = await make_get_request(f'/api/v1/films/{film_id}', headers=auth_header(roles=['admin']))
        assert response.status == 200

    async def test_premium_plain_user_forbidden(self, make_get_request, public_and_premium_films):
        """Non-subscriber токен: Auth недоступен -> деградация к ролям токена -> 403 (не 5xx)."""
        film_id = public_and_premium_films['premium']['id']
        response = await make_get_request(f'/api/v1/films/{film_id}', headers=auth_header(roles=['user']))
        assert response.status == 403


class TestGracefulDegradation:
    """Падение Auth-сервиса не выводит из строя сервис выдачи контента."""

    async def test_catalog_available_when_auth_down(self, make_get_request, public_and_premium_films):
        # Auth недоступен в тестовой сети — каталог всё равно отвечает.
        response = await make_get_request('/api/v1/films')
        assert response.status == 200
        assert response.status < 500

    async def test_subscriber_served_from_token_when_auth_down(self, make_get_request, public_and_premium_films):
        # Быстрый путь: роль подписчика в токене -> доступ без обращения к Auth.
        film_id = public_and_premium_films['premium']['id']
        response = await make_get_request(f'/api/v1/films/{film_id}', headers=auth_header(roles=['subscriber']))
        assert response.status == 200

    async def test_no_5xx_for_missing_premium_film(self, make_get_request, public_and_premium_films):
        response = await make_get_request(f'/api/v1/films/{uuid.uuid4()}', headers=auth_header(roles=['subscriber']))
        assert response.status == 404
