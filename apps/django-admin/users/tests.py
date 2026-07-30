"""Тесты бэкенда аутентификации через Auth-сервис (с моками HTTP)."""

from unittest import mock

import requests
from django.contrib.auth import get_user_model
from django.test import TestCase

from users.auth_backend import AuthServiceBackend

User = get_user_model()

USER_ID = '11111111-1111-1111-1111-111111111111'


def _resp(status_code, json_data=None):
    m = mock.Mock()
    m.status_code = status_code
    m.json.return_value = json_data or {}
    return m


class AuthServiceBackendTests(TestCase):
    def setUp(self):
        self.backend = AuthServiceBackend()

    @mock.patch('users.auth_backend.requests.request')
    def test_authenticate_success_creates_user(self, req):
        req.side_effect = [
            _resp(200, {'access_token': 'tok', 'refresh_token': 'r'}),
            _resp(
                200,
                {
                    'id': USER_ID,
                    'login': 'alice',
                    'email': 'alice@example.com',
                    'roles': [{'name': 'user'}],
                },
            ),
        ]
        user = self.backend.authenticate(None, username='alice', password='pw')
        assert user is not None
        assert str(user.id) == USER_ID
        assert user.login == 'alice'
        assert user.email == 'alice@example.com'
        assert user.is_active is True
        # Роль user не даёт прав администратора.
        assert user.is_staff is False
        assert user.is_superuser is False

    @mock.patch('users.auth_backend.requests.request')
    def test_authenticate_admin_role_is_superuser(self, req):
        req.side_effect = [
            _resp(200, {'access_token': 'tok'}),
            _resp(200, {'id': USER_ID, 'login': 'boss', 'email': '', 'roles': [{'name': 'admin'}]}),
        ]
        user = self.backend.authenticate(None, username='boss', password='pw')
        assert user.is_superuser is True
        assert user.is_staff is True

    @mock.patch('users.auth_backend.requests.request')
    def test_authenticate_updates_existing_user(self, req):
        User.objects.create(id=USER_ID, login='old', email='old@example.com')
        req.side_effect = [
            _resp(200, {'access_token': 'tok'}),
            _resp(
                200, {'id': USER_ID, 'login': 'newlogin', 'email': 'new@example.com', 'roles': [{'name': 'superuser'}]}
            ),
        ]
        user = self.backend.authenticate(None, username='newlogin', password='pw')
        assert user.login == 'newlogin'
        assert user.is_superuser is True
        assert User.objects.count() == 1

    @mock.patch('users.auth_backend.requests.request')
    def test_bad_credentials_returns_none(self, req):
        req.side_effect = [_resp(401, {'detail': 'bad'})]
        user = self.backend.authenticate(None, username='alice', password='wrong')
        assert user is None
        assert User.objects.count() == 0

    @mock.patch('users.auth_backend.requests.request')
    def test_auth_service_down_returns_none(self, req):
        # Все попытки падают с сетевой ошибкой -> деградация (следующий бэкенд).
        req.side_effect = requests.ConnectionError('auth down')
        user = self.backend.authenticate(None, username='alice', password='pw')
        assert user is None
        assert User.objects.count() == 0

    def test_missing_credentials_returns_none(self):
        assert self.backend.authenticate(None, username=None, password=None) is None

    def test_get_user(self):
        User.objects.create(id=USER_ID, login='alice')
        fetched = self.backend.get_user(USER_ID)
        assert fetched is not None
        assert str(fetched.id) == USER_ID
        assert self.backend.get_user('22222222-2222-2222-2222-222222222222') is None
