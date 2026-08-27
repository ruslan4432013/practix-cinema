"""Настройки для юнит-тестов: SQLite в памяти, без внешней инфраструктуры.

Нужны потому, что ``npx nx run-many -t test`` выполняется на голом раннере, где
ни Postgres, ни RabbitMQ, ни SMTP нет. Ровно поэтому в моделях нет ни
``ArrayField``, ни других постгресовых полей: их появление молча превратило бы
юнит-тесты в функциональные.

Функциональные тесты этот модуль НЕ используют — они идут против настоящего
Postgres и настоящего брокера в ``docker-compose.test.yml``.
"""

from practix_notifications.settings import *  # noqa: F403 - это переопределение, а не самостоятельный конфиг

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}

# Хеширование пароля на каждую фикстуру пользователя — самая дорогая операция в
# юнит-тестах Django и единственная, которая ничего здесь не проверяет.
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']

# Манифест статики требует собранного collectstatic, которого в юнит-тестах нет.
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}
