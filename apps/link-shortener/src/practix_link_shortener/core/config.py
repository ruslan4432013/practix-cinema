"""Конфигурация сервиса сокращения ссылок.

Стиль именования — UPPER_SNAKE_CASE, как в Auth, коллекторе, UGC и
нотификациях (в Movies API принят противоположный; см. CLAUDE.md — не смешивать).

ПРЕФИКС ``SHORTENER_``. Все контейнеры читают ОДИН корневой ``.env``, поэтому
префикс обязан быть свободным: ``UGC_`` занят коллектором, ``UGC_API_`` — UGC,
``NOTIFY_`` — нотификациями, ``NOTIFY_WS_`` — websocket-шлюзом, ``ETL_``/``CH_``
— ETL. ``SHORTENER_`` не занят ничем.

Логгер отсюда не вызывается: в Auth и Movies API ``core/config.py`` дёргает
``setup_logging()`` на импорте, и из-за этого настройки нельзя импортировать
внутри логгера — цикл. Здесь логирование настраивается из ``main.py``.
"""

from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from practix_core.settings import validate_environment

# Значения по умолчанию годятся только для локальной разработки и тестов.
# Опасность таких значений в том, что они работают: сервис поднимается, ничего
# не ломается, и подмену забывают. Поэтому в продакшене старт с ними запрещён.
INSECURE_DEFAULT_INTERNAL_TOKEN = 'change-me-shortener-token'
INSECURE_DEFAULT_AUTH_PASSWORD = 'change-me'


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    PROJECT_NAME: str = 'link-shortener'
    SHORTENER_ENV: str = 'dev'

    # --- Хранилище ---
    SHORTENER_POSTGRES_USER: str = 'postgres'
    SHORTENER_POSTGRES_PASSWORD: str = 'secret'
    SHORTENER_POSTGRES_DB: str = 'shortener_database'
    SHORTENER_POSTGRES_HOST: str = '127.0.0.1'
    SHORTENER_POSTGRES_PORT: int = 5432
    SHORTENER_DB_ECHO: bool = False
    SHORTENER_DB_POOL_SIZE: int = 10
    SHORTENER_DB_MAX_OVERFLOW: int = 20

    # --- Форма самой ссылки ---
    # База, которую сервис отдаёт вызывающему. Это ВНЕШНИЙ адрес (через nginx), а
    # не адрес контейнера: ссылка уходит в письмо и открывается на телефоне.
    SHORTENER_PUBLIC_BASE_URL: str = 'http://localhost'
    # Должен совпадать с `location ^~ /s/` в infra/nginx/configs/site.conf.
    SHORTENER_REDIRECT_PATH: str = '/s'
    # 62^7 ≈ 3.5·10^12. Длина — компромисс между «помещается в SMS» и «не
    # перебирается»; ссылка подтверждения security-bearing, поэтому короче нельзя.
    SHORTENER_CODE_LENGTH: int = Field(default=7, ge=6, le=16)
    SHORTENER_CODE_MAX_ATTEMPTS: int = Field(default=5, ge=1, le=20)
    SHORTENER_DEFAULT_TTL_HOURS: int = Field(default=72, ge=1)
    SHORTENER_MAX_TTL_HOURS: int = Field(default=720, ge=1)

    # --- Уборка протухших ссылок ---
    # Размер пачки — компромисс между числом транзакций и длиной блокировки:
    # один DELETE на всю таблицу держал бы блокировки на всех удаляемых строках,
    # пока по ним же ходит счётчик визитов горячего редиректа.
    SHORTENER_PURGE_BATCH: int = Field(default=1000, ge=1, le=100_000)
    # Пауза между пачками. По умолчанию 0.0 — уборка идёт вплотную; на нагруженном
    # стенде ненулевая пауза разводит её с горячими редиректами и даёт autovacuum
    # успевать за освобождёнными страницами.
    SHORTENER_PURGE_SLEEP: float = Field(default=0.0, ge=0.0)

    # --- Защита от открытого редиректа ---
    # Куда РАЗРЕШЕНО уводить. Пустой список означал бы «куда угодно», то есть
    # готовый инструмент фишинга с нашего домена в адресной строке.
    SHORTENER_ALLOWED_REDIRECT_HOSTS: str = 'localhost,127.0.0.1'
    # Куда ведёт кнопка «на главную» со страницы 404.
    SHORTENER_PUBLIC_SITE_URL: str = 'http://localhost'

    # --- Ручка создания ссылок (межсервисная) ---
    # Общий секрет, а не JWT: вызывающий — сервис, а не человек, и заводить ради
    # него учётку с ролями значило бы держать второй механизм авторизации.
    # Снаружи сети ручка недостижима и без него (у неё нет маршрута в nginx),
    # но «недостижима снаружи» и «может звать кто угодно изнутри» — разные вещи.
    SHORTENER_INTERNAL_TOKEN: str = INSECURE_DEFAULT_INTERNAL_TOKEN

    # --- Интеграция с Auth (подтверждение адреса) ---
    SHORTENER_AUTH_API_URL: str = 'http://auth:8000'
    SHORTENER_AUTH_SERVICE_LOGIN: str = 'svc-link-shortener'
    SHORTENER_AUTH_SERVICE_PASSWORD: str = INSECURE_DEFAULT_AUTH_PASSWORD
    SHORTENER_AUTH_TIMEOUT: float = 5.0
    SHORTENER_AUTH_MAX_ATTEMPTS: int = Field(default=3, ge=1, le=10)

    # --- Distributed tracing (OpenTelemetry / Jaeger) ---
    OTEL_ENABLED: bool = True
    OTEL_SERVICE_NAME: str = 'link-shortener'
    OTEL_EXPORTER_OTLP_ENDPOINT: str = 'http://jaeger:4318'

    LOG_LEVEL: str = 'INFO'
    LOG_JSON: bool = True

    # --- Сбор ошибок (Sentry / GlitchTip) ---
    # Префикс БЕЗ SHORTENER_: ключи общие на весь стенд, один приёмник на все
    # сервисы, а различает их тег service (см. practix_core.sentry).
    SENTRY_ENABLED: bool = False
    SENTRY_DSN: str = ''
    SENTRY_ENVIRONMENT: str = 'dev'
    SENTRY_RELEASE: str = ''
    SENTRY_SAMPLE_RATE: float = 1.0
    SENTRY_SEND_DEFAULT_PII: bool = False

    @model_validator(mode='after')
    def _validate_production_hardening(self) -> 'Settings':
        """Не даём подняться в продакшене с небезопасными настройками."""
        validate_environment(self.SHORTENER_ENV, key='SHORTENER_ENV', insecure_defaults=self.insecure_defaults)
        return self

    @property
    def insecure_defaults(self) -> list[str]:
        """Секреты, оставшиеся в значении по умолчанию (выводятся в лог при старте)."""
        problems = []
        if self.SHORTENER_INTERNAL_TOKEN == INSECURE_DEFAULT_INTERNAL_TOKEN:
            problems.append(
                'SHORTENER_INTERNAL_TOKEN is the built-in default — anyone inside the network can mint links'
            )
        if self.SHORTENER_AUTH_SERVICE_PASSWORD == INSECURE_DEFAULT_AUTH_PASSWORD:
            problems.append('SHORTENER_AUTH_SERVICE_PASSWORD is the built-in default — the service account is public')
        return problems

    @property
    def database_url(self) -> str:
        return (
            f'postgresql+asyncpg://{self.SHORTENER_POSTGRES_USER}:{self.SHORTENER_POSTGRES_PASSWORD}'
            f'@{self.SHORTENER_POSTGRES_HOST}:{self.SHORTENER_POSTGRES_PORT}/{self.SHORTENER_POSTGRES_DB}'
        )

    @property
    def allowed_redirect_hosts(self) -> frozenset[str]:
        """Белый список хостов редиректа, нормализованный к нижнему регистру."""
        return frozenset(
            host.strip().lower() for host in self.SHORTENER_ALLOWED_REDIRECT_HOSTS.split(',') if host.strip()
        )

    @property
    def redirect_prefix(self) -> str:
        """Префикс короткой ссылки без хвостового слэша: ``http://localhost/s``."""
        return f'{self.SHORTENER_PUBLIC_BASE_URL.rstrip("/")}/{self.SHORTENER_REDIRECT_PATH.strip("/")}'

    @property
    def public_host(self) -> str:
        """Хост, на котором живут наши же короткие ссылки (нужен, чтобы не зациклить редирект)."""
        return (urlsplit(self.SHORTENER_PUBLIC_BASE_URL).hostname or '').lower()


settings = Settings()
