"""Конфигурация Django-сервиса на базе pydantic-settings.

Все значения, которые раньше читались через ``os.getenv``/``os.environ`` в
``settings.py`` и ``wsgi.py``, теперь описаны здесь единым типизированным классом.
Имена полей совпадают с именами переменных окружения (UPPER_SNAKE_CASE), поэтому
алиасы не нужны. Значения по умолчанию идентичны прежнему поведению.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_file_encoding='utf-8',
        extra='ignore',
    )

    # --- Django core ---
    SECRET_KEY: str = 'secret_key'
    DEBUG: bool = False

    # --- Database ---
    SQL_ENGINE: str = 'django.db.backends.postgresql'
    SQL_HOST: str = '127.0.0.1'
    SQL_PORT: int = 5432
    SQL_OPTIONS: str | None = None
    POSTGRES_DB: str | None = None
    POSTGRES_USER: str | None = None
    POSTGRES_PASSWORD: str | None = None

    # --- Auth-service integration ---
    AUTH_API_URL: str = 'http://auth:8000'
    AUTH_API_TIMEOUT: float = 2.0
    AUTH_API_MAX_ATTEMPTS: int = 3

    # --- Логи ---
    # Те же ключи, что у остальных сервисов стенда, и тот же корневой .env:
    # JSON собирает сборщик логов, текст удобнее при локальной отладке.
    LOG_LEVEL: str = 'INFO'
    LOG_JSON: bool = True

    # --- Distributed tracing (OpenTelemetry / Jaeger) ---
    OTEL_ENABLED: bool = True
    OTEL_EXPORTER_OTLP_ENDPOINT: str = 'http://jaeger:4318'
    OTEL_SERVICE_NAME: str = 'django-admin'

    # --- Сбор ошибок (Sentry / GlitchTip) ---
    # Ключи те же, что у остальных сервисов стенда: приёмник один, различает
    # сервисы тег `service`. Выключено по умолчанию — без поднятого приёмника
    # SDK молотил бы в пустоту.
    SENTRY_ENABLED: bool = False
    SENTRY_DSN: str = ''
    SENTRY_ENVIRONMENT: str = 'dev'
    SENTRY_RELEASE: str = ''
    SENTRY_SAMPLE_RATE: float = 1.0
    SENTRY_SEND_DEFAULT_PII: bool = False


settings = Settings()
