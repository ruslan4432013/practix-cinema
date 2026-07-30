import uuid

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models

from users.managers import UserManager


class User(AbstractBaseUser, PermissionsMixin):
    """Модель пользователя админ-панели.

    Идентификатор совпадает с `sub` (UUID) из Auth-сервиса, что позволяет
    держать локального пользователя консистентным с SSO-пользователем.
    Аутентификация делегируется Auth-сервису (см. `users.auth_backend`),
    но локальные пользователи тоже поддерживаются как fallback.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    login = models.CharField(max_length=255, unique=True)
    email = models.EmailField(max_length=255, blank=True)
    first_name = models.CharField(max_length=255, blank=True)
    last_name = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = UserManager()

    USERNAME_FIELD = 'login'
    REQUIRED_FIELDS = []

    class Meta:
        verbose_name = 'user'
        verbose_name_plural = 'users'

    def __str__(self):
        return f'{self.login} ({self.id})'
