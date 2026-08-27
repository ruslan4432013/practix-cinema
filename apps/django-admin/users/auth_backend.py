"""Бэкенд аутентификации Django через Auth-сервис (SSO).

Изящная деградация: любая недоступность Auth-сервиса (таймаут, сетевая
ошибка, 5xx) приводит к возврату ``None`` из ``authenticate``. Благодаря
порядку в ``AUTHENTICATION_BACKENDS`` Django после этого пробует
``ModelBackend`` — локальный суперпользователь сможет войти, даже если
Auth-сервис лежит. Падение Auth не выводит из строя админ-панель.
"""

import logging

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import BaseBackend
from example.http_retry import request_with_retry

logger = logging.getLogger(__name__)

User = get_user_model()

# Роли Auth-сервиса, дающие права администратора/доступ в админку.
SUPERUSER_ROLES = {'admin', 'superuser'}
STAFF_ROLES = {'admin', 'superuser'}


def _request_with_retry(method: str, url: str, **kwargs) -> requests.Response:
    """HTTP-запрос к Auth-сервису с ретраями по его настройкам таймаута."""
    return request_with_retry(
        method,
        url,
        attempts=getattr(settings, 'AUTH_API_MAX_ATTEMPTS', 3),
        timeout=getattr(settings, 'AUTH_API_TIMEOUT', 2.0),
        **kwargs,
    )


class AuthServiceBackend(BaseBackend):
    def authenticate(self, request, username=None, password=None):
        if not username or not password:
            return None

        base = settings.AUTH_API_URL.rstrip('/')

        # Auth-сервис требует X-Request-Id: прокидываем идентификатор текущего
        # запроса на межсервисные вызовы (traceparent OTel добавит сам).
        rid_headers = {}
        request_id = request.META.get('HTTP_X_REQUEST_ID') if request is not None else None
        if request_id:
            rid_headers['X-Request-Id'] = request_id

        # 1) Логин в Auth-сервисе (аутентификация по login + password).
        try:
            login_resp = _request_with_retry(
                'POST',
                f'{base}/api/v1/auth/login',
                json={'login': username, 'password': password},
                headers=rid_headers,
            )
        except requests.RequestException:
            return None  # Auth недоступен -> деградация к следующему бэкенду

        if login_resp.status_code != 200:
            return None  # неверные учётные данные (валидный ответ Auth)

        access_token = login_resp.json().get('access_token')
        if not access_token:
            return None

        # 2) Профиль пользователя (id, email, роли).
        try:
            me_resp = _request_with_retry(
                'GET',
                f'{base}/api/v1/users/me',
                headers={'Authorization': f'Bearer {access_token}', **rid_headers},
            )
        except requests.RequestException:
            return None

        if me_resp.status_code != 200:
            return None

        data = me_resp.json()
        roles = {r.get('name') for r in data.get('roles', [])}
        is_superuser = bool(roles & SUPERUSER_ROLES)
        is_staff = bool(roles & STAFF_ROLES)

        # 3) Синхронизируем локального пользователя (создаём/обновляем).
        try:
            user, _ = User.objects.update_or_create(
                id=data['id'],
                defaults={
                    'login': data.get('login', username),
                    'email': data.get('email') or '',
                    'is_superuser': is_superuser,
                    'is_staff': is_staff,
                    'is_active': True,
                },
            )
        except Exception:
            logger.exception('Не удалось синхронизировать локального пользователя из Auth-сервиса')
            return None

        return user

    def get_user(self, user_id):
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None
