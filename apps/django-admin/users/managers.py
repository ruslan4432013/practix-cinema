from django.contrib.auth.base_user import BaseUserManager


class UserManager(BaseUserManager):
    """Менеджер пользователя, использующий `login` как идентификатор.

    Нужен для локальных пользователей (fallback-суперпользователь Django,
    работающий через ModelBackend, когда Auth-сервис недоступен).
    """

    def create_user(self, login, password=None, **extra_fields):
        if not login:
            raise ValueError('У пользователя должен быть login')
        user = self.model(login=login, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, login, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('is_active', True)

        if extra_fields.get('is_staff') is not True:
            raise ValueError('Суперпользователь должен иметь is_staff=True')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Суперпользователь должен иметь is_superuser=True')

        return self.create_user(login, password, **extra_fields)
