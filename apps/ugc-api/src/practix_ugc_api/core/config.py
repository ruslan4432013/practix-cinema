"""Конфигурация сервиса пользовательского контента.

Стиль именования — UPPER_SNAKE_CASE, как в Auth и коллекторе (в Movies API
принят противоположный, lower_snake_case; см. CLAUDE.md — не смешивать).

ПРЕФИКС ``UGC_API_``, А НЕ ``UGC_``. Голый ``UGC_`` уже принадлежит
analytics-collector — там около двадцати пяти ключей (``UGC_REDIS_*``,
``UGC_RATE_LIMIT_*``, ``UGC_FALLBACK_*``). Оба контейнера читают ОДИН корневой
``.env``, поэтому совпадение имени означало бы, что одна переменная управляет
двумя разными сервисами.

Логгер отсюда не вызывается. В Auth и Movies API ``core/config.py`` дёргает
``setup_logging()`` на импорте, и из-за этого настройки нельзя импортировать
внутри логгера — цикл. ``docs/monorepo.md`` числит это известной болячкой;
здесь ``setup_logging()`` вызывается из ``main.py``, и цикла нет.
"""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Значение по умолчанию годится только для локальной разработки и тестов.
# Опасность таких значений в том, что они работают: сервис поднимается, ничего
# не ломается, и подмену забывают. Поэтому в продакшене старт с ним запрещён.
INSECURE_DEFAULT_JWT_SECRET = 'secret'

KNOWN_ENVIRONMENTS = frozenset({'dev', 'test', 'prod'})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    PROJECT_NAME: str = 'ugc-api'
    UGC_API_ENV: str = 'dev'

    # --- Хранилище ---
    UGC_API_POSTGRES_USER: str = 'postgres'
    UGC_API_POSTGRES_PASSWORD: str = 'secret'
    UGC_API_POSTGRES_DB: str = 'ugc_database'
    UGC_API_POSTGRES_HOST: str = '127.0.0.1'
    UGC_API_POSTGRES_PORT: int = 5432
    # echo=True в Auth включён и в продакшене — это ошибка, которую здесь не
    # повторяем: построчный лог SQL на каждый запрос стоит дороже самих запросов.
    UGC_API_DB_ECHO: bool = False
    UGC_API_DB_POOL_SIZE: int = 10
    UGC_API_DB_MAX_OVERFLOW: int = 20

    # --- Продуктовые правила ---
    # Порог, с которого оценка считается лайком. Он ПРОДУКТОВЫЙ и будет меняться,
    # поэтому лайки и дизлайки не хранятся счётчиками, а выводятся из гистограммы
    # оценок за O(1). Ровно ради этого в исследовании выбрана гистограмма из 11
    # чисел: пара счётчиков потребовала бы пересчёта по всей таблице при каждой
    # смене порога (см. research/ugc-storage/README.md).
    UGC_API_LIKE_THRESHOLD: int = Field(default=6, ge=0, le=10)
    UGC_API_REVIEW_MAX_LENGTH: int = 4000
    # Верхняя граница выборки «понравившиеся фильмы» по умолчанию — оценка ≥ 8,
    # тот же порог, на котором мерился сценарий R1.
    UGC_API_LIKED_MIN_RATING: int = Field(default=8, ge=0, le=10)

    # --- Интеграция с Auth (проверка JWT локальная, без сетевых вызовов) ---
    AUTHJWT_SECRET_KEY: str = INSECURE_DEFAULT_JWT_SECRET
    AUTHJWT_DENYLIST_ENABLED: bool = True
    AUTHJWT_DENYLIST_TOKEN_CHECKS: set[str] = {'access'}
    # Роли, которым разрешено удалять чужие рецензии.
    SUPERUSER_ROLES: set[str] = {'admin', 'superuser'}

    # Денилист отозванных токенов ведёт Auth, и читать его надо там же, где он
    # пишется, — в Redis Auth-сервиса (база 0). Своего Redis у сервиса нет.
    REDIS_HOST: str = '127.0.0.1'
    REDIS_PORT: int = 6379
    AUTH_REDIS_HOST: str | None = None
    AUTH_REDIS_PORT: int | None = None
    AUTH_REDIS_DB: int = 0

    # --- Distributed tracing (OpenTelemetry / Jaeger) ---
    OTEL_ENABLED: bool = True
    OTEL_SERVICE_NAME: str = 'ugc-api'
    OTEL_EXPORTER_OTLP_ENDPOINT: str = 'http://jaeger:4318'

    LOG_LEVEL: str = 'INFO'
    LOG_JSON: bool = True

    # --- Сбор ошибок (Sentry / GlitchTip) ---
    # Выключено по умолчанию: без поднятого приёмника SDK молотил бы в пустоту.
    # Префикс здесь БЕЗ UGC_API_: ключи общие на весь стенд, один приёмник на
    # все сервисы, а различает их тег service (см. practix_core.sentry).
    SENTRY_ENABLED: bool = False
    SENTRY_DSN: str = ''
    SENTRY_ENVIRONMENT: str = 'dev'
    SENTRY_RELEASE: str = ''
    SENTRY_SAMPLE_RATE: float = 1.0
    SENTRY_SEND_DEFAULT_PII: bool = False

    @model_validator(mode='after')
    def _validate_production_hardening(self) -> 'Settings':
        """Не даём подняться в продакшене с небезопасными настройками."""
        normalized = self.UGC_API_ENV.strip().lower()
        if normalized not in KNOWN_ENVIRONMENTS:
            raise ValueError(f'UGC_API_ENV must be one of {sorted(KNOWN_ENVIRONMENTS)}, got {self.UGC_API_ENV!r}')
        if normalized == 'prod' and self.insecure_defaults:
            raise ValueError(
                'Insecure default values must be overridden in production: ' + '; '.join(self.insecure_defaults)
            )
        return self

    @property
    def insecure_defaults(self) -> list[str]:
        """Секреты, оставшиеся в значении по умолчанию (выводятся в лог при старте)."""
        if self.AUTHJWT_SECRET_KEY == INSECURE_DEFAULT_JWT_SECRET:
            return ['AUTHJWT_SECRET_KEY is the built-in default — anyone can forge an access token']
        return []

    @property
    def database_url(self) -> str:
        return (
            f'postgresql+asyncpg://{self.UGC_API_POSTGRES_USER}:{self.UGC_API_POSTGRES_PASSWORD}'
            f'@{self.UGC_API_POSTGRES_HOST}:{self.UGC_API_POSTGRES_PORT}/{self.UGC_API_POSTGRES_DB}'
        )

    @property
    def auth_redis_host(self) -> str:
        return self.AUTH_REDIS_HOST or self.REDIS_HOST

    @property
    def auth_redis_port(self) -> int:
        return self.AUTH_REDIS_PORT or self.REDIS_PORT


settings = Settings()
