"""Герметичное окружение для юнит-набора Auth.

Настройки сервиса обязательны почти целиком — у них нет значений по умолчанию, и
это правильно: Auth не должен подниматься на «секретах по умолчанию». Но юнит-тесты
запускаются на голом раннере, где ни ``.env``, ни контейнеров нет, и без этого
модуля падал бы сам импорт ``practix_auth.core.config``.

Значения ниже фиктивные и никуда не ходят: набор проверяет поведение кода, а не
подключения. Переменные окружения выставляются ДО первого импорта пакета, потому
что ``settings`` создаётся на импорте модуля конфигурации.
"""

import os

_FAKE_ENV = {
    'PROJECT_NAME': 'auth-tests',
    'REDIS_HOST': 'localhost',
    'REDIS_PORT': '6379',
    'AUTH_POSTGRES_DB': 'auth_database',
    'AUTH_POSTGRES_USER': 'postgres',
    'AUTH_POSTGRES_PASSWORD': 'secret',
    'AUTH_POSTGRES_HOST': 'localhost',
    'AUTH_POSTGRES_PORT': '5432',
    'JWT_SECRET_KEY': 'test-secret',
    'JWT_ALGORITHM': 'HS256',
    'ACCESS_TOKEN_EXPIRE_MINUTES': '15',
    'REFRESH_TOKEN_EXPIRE_DAYS': '7',
    'BASE_DIR': '/tmp',
    'AUTHJWT_SECRET_KEY': 'test-secret',
    'AUTHJWT_DENYLIST_ENABLED': 'True',
    'AUTHJWT_DENYLIST_TOKEN_CHECKS': '["access","refresh"]',
    'ACCESS_TOKEN_EXPIRES': '900',
    'REFRESH_TOKEN_EXPIRES': '604800',
    'DEFAULT_ROLE_NAME': 'user',
    'SUPERUSER_ROLES': '["admin","superuser"]',
}

# setdefault, а не присваивание: локальный .env разработчика имеет право
# победить, и подменять его молча незачем.
for key, value in _FAKE_ENV.items():
    os.environ.setdefault(key, value)
