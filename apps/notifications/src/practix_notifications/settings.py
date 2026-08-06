"""Настройки Django для сервиса нотификаций.

Значения берутся из ``core.config.settings`` (pydantic-settings, корневой
``.env``) — так же, как это сделано в ``apps/django-admin/example/config.py``.
Прямых обращений к ``os.environ`` здесь нет намеренно: единственный источник
истины про окружение — модель настроек, и она же валидирует его на старте.

``LOGGING`` собирается ``practix_core.logging.build_logging_config`` — той же
функцией, что у пяти остальных сервисов. Порядок ключей в записи
(``timestamp, level, logger, message, request_id, service``) — контракт, на
который опираются Logstash и Kibana, и он закреплён тестом в
``libs/platform-core/tests/test_logging.py``. ``django-admin`` эту функцию
импортировать не может (он вне workspace'а) и держит форк формата в
``example/logging_json.py`` — здесь форка нет.
"""

from pathlib import Path

from practix_core.logging import build_logging_config
from practix_notifications.core.config import settings

BASE_DIR = Path(__file__).resolve().parent

SECRET_KEY = settings.NOTIFY_SECRET_KEY
DEBUG = settings.NOTIFY_DEBUG
ALLOWED_HOSTS = settings.allowed_hosts
CSRF_TRUSTED_ORIGINS = settings.csrf_trusted_origins

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'practix_notifications.subscribers',
    'practix_notifications.content',
    'practix_notifications.campaigns',
    'practix_notifications.inbox',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # Идентификатор запроса ставится ДО всего остального: он должен попасть в
    # каждую строку лога, включая логи самих middleware ниже.
    'practix_notifications.core.middleware.RequestIdMiddleware',
    # whitenoise отдаёт статику самим процессом. django-admin получает её от
    # Nginx через общий том static_volume, а мы за Nginx не стоим — без этой
    # строки админка рендерится без единого стиля.
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'practix_notifications.urls'
WSGI_APPLICATION = 'practix_notifications.wsgi.application'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': settings.NOTIFY_POSTGRES_DB,
        'USER': settings.NOTIFY_POSTGRES_USER,
        'PASSWORD': settings.NOTIFY_POSTGRES_PASSWORD,
        'HOST': settings.NOTIFY_POSTGRES_HOST,
        'PORT': settings.NOTIFY_POSTGRES_PORT,
        # Воркер и планировщик — долгоживущие процессы, и переподключение на
        # каждый запрос им обходится дороже, чем панели.
        'CONN_MAX_AGE': settings.NOTIFY_DB_CONN_MAX_AGE,
    }
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'static'
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'},
}

LANGUAGE_CODE = 'ru-ru'
# Всё внутри системы живёт в UTC, а локальное время получателя вычисляется из
# его ``subscriber.timezone`` в момент отправки. Иначе «не писать ночью»
# означало бы «не писать ночью по времени сервера», что бессмысленно.
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

LOGGING = build_logging_config(
    level=settings.LOG_LEVEL,
    json_output=settings.LOG_JSON,
    static_fields={'service': settings.OTEL_SERVICE_NAME},
    logger_levels={
        # На INFO Django печатает каждый SQL-запрос вместе с параметрами — при
        # рассылке на десятки тысяч адресов это больше данных, чем сама рассылка.
        'django.db.backends': 'WARNING',
        'django.request': settings.LOG_LEVEL,
        # pika на INFO комментирует каждый кадр протокола.
        'pika': 'WARNING',
        'notifications': settings.LOG_LEVEL,
    },
)
