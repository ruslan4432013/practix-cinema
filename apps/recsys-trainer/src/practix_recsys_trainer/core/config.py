"""Конфигурация офлайн-обучения.

ПРЕФИКС ``RECS_TRAINER_`` для собственных ручек батча и ``RECS_`` для витрины —
теми же именами, что и в ``recommendations-api``. Витрина одна, и связность к
ней обязана описываться ОДНИМ набором ключей: второй набор на то же соединение
— это ровно тот способ, которым конфигурация расходится с реальностью, причём
молча.

Ключи чужих источников тоже не переизобретаются: ClickHouse читается через уже
существующие ``CH_*``, каталог — через ``POSTGRES_*``, оценки — через
``UGC_API_POSTGRES_*``. Все контейнеры делят один корневой ``.env``, и завести
``RECS_TRAINER_CLICKHOUSE_HOST`` рядом с ``CH_HOSTS`` значило бы получить два
адреса одного кластера, которые однажды разъедутся.

Чтение чужих баз напрямую — не нарушение принципа «сервис владеет своими
данными», а тот же офлайновый доступ, что у ``etl-elasticsearch``, который
читает ``theatre-db`` django-админки. Соединения read-only, на горячем пути их
нет.
"""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from practix_core.settings import validate_environment


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    PROJECT_NAME: str = 'recsys-trainer'
    RECS_TRAINER_ENV: str = 'dev'

    # --- Витрина (те же ключи, что у recommendations-api) ---
    RECS_POSTGRES_USER: str = 'postgres'
    RECS_POSTGRES_PASSWORD: str = 'secret'
    RECS_POSTGRES_DB: str = 'recs_database'
    RECS_POSTGRES_HOST: str = '127.0.0.1'
    RECS_POSTGRES_PORT: int = 5432

    RECS_REDIS_HOST: str = '127.0.0.1'
    RECS_REDIS_PORT: int = 6379
    RECS_REDIS_DB: int = 0
    RECS_REDIS_TIMEOUT: float = 1.0
    RECS_CACHE_TTL: int = Field(default=172_800, ge=60)

    # --- Источник неявного сигнала: ClickHouse (ADR-005) ---
    # CH_HOSTS в боевом стенде — список 'clickhouse-01:8123,clickhouse-03:8123'
    # (по одной реплике на шард). Батч берёт первый живой: ему не нужен кворум
    # чтения, ему нужен любой узел, отвечающий на SELECT.
    CH_HOSTS: str = 'clickhouse-01:8123'
    CH_USER: str = 'etl'
    CH_PASSWORD: str = 'etl'
    CH_DATABASE: str = 'ugc'
    CH_CONNECT_TIMEOUT: int = 10
    CH_SEND_RECEIVE_TIMEOUT: int = 300

    # --- Источник явного сигнала: оценки в ugc-db ---
    UGC_API_POSTGRES_USER: str = 'postgres'
    UGC_API_POSTGRES_PASSWORD: str = 'secret'
    UGC_API_POSTGRES_DB: str = 'ugc_database'
    UGC_API_POSTGRES_HOST: str = 'ugc-db'
    UGC_API_POSTGRES_PORT: int = 5432
    RECS_TRAINER_RATINGS_ENABLED: bool = True

    # --- Каталог: theatre-db ---
    POSTGRES_USER: str = 'app'
    POSTGRES_PASSWORD: str = '123qwe'
    POSTGRES_DB: str = 'theatre'
    POSTGRES_HOST: str = 'theatre-db'
    POSTGRES_PORT: int = 5432

    # --- Правила обучения ---
    # Ниже этой доли просмотра взаимодействие не считается сигналом: открыл
    # карточку, посмотрел десять секунд и ушёл — это не «смотрел фильм».
    RECS_TRAINER_MIN_COMPLETION: float = Field(default=0.1, ge=0.0, le=1.0)
    # Пользователи с одним фильмом не дают со-встречаемости по определению
    # (пара нужна для пары), но раздувают матрицу и портят метрики.
    RECS_TRAINER_MIN_USER_EVENTS: int = Field(default=2, ge=1)
    # Фильмы, которые смотрел один человек, дают «соседей» из шума.
    RECS_TRAINER_MIN_FILM_EVENTS: int = Field(default=2, ge=1)
    # Сколько соседей хранить на фильм и позиций на пользователя.
    RECS_TRAINER_TOP_N: int = Field(default=20, ge=1, le=200)
    RECS_TRAINER_POPULAR_LIMIT: int = Field(default=100, ge=1, le=1000)
    RECS_TRAINER_POPULAR_WINDOW_DAYS: int = Field(default=30, ge=1)
    # Верхняя граница выборки из ClickHouse за один прогон. Не защита от
    # переполнения памяти (разреженная матрица на порядки компактнее), а
    # предохранитель от того, чтобы ночной батч читал терабайт при опечатке.
    RECS_TRAINER_MAX_INTERACTIONS: int = Field(default=5_000_000, ge=1000)
    # Сколько пользователей получают персональную выдачу. 200 000 × 20 позиций
    # ≈ 200 МБ — предел, за которым витрину пора шардировать по user_id.
    RECS_TRAINER_MAX_PERSONAL_USERS: int = Field(default=200_000, ge=1)

    # --- ALS (E6) ---
    RECS_TRAINER_ALS_ENABLED: bool = True
    RECS_TRAINER_ALS_FACTORS: int = Field(default=64, ge=2, le=512)
    RECS_TRAINER_ALS_ITERATIONS: int = Field(default=15, ge=1, le=200)
    RECS_TRAINER_ALS_REGULARIZATION: float = Field(default=0.05, gt=0.0)
    # Множитель уверенности в неявном сигнале (alpha из статьи Hu-Koren-Volinsky).
    RECS_TRAINER_ALS_ALPHA: float = Field(default=40.0, gt=0.0)
    RECS_TRAINER_ALS_SEED: int = 42

    # --- Раскладка витрины ---
    # Сколько строк в одной вставке. Витрина на 200k пользователей — это 4 млн
    # строк, и одна транзакция на всё держала бы блокировки и раздувала WAL
    # ровно так же, как неограниченный DELETE в шортенере.
    RECS_TRAINER_WRITE_BATCH: int = Field(default=10_000, ge=100, le=200_000)
    # Сколько прошлых версий оставлять. Ноль означал бы удаление той, на которую
    # смотрит выдача, — от этого защищает ещё и ON DELETE RESTRICT.
    RECS_TRAINER_KEEP_VERSIONS: int = Field(default=2, ge=1, le=50)
    # Сколько фильмов прогреть в Redis после переключения указателя. Прогревать
    # всё бессмысленно: хвост каталога никто не запрашивает, а TTL всё равно
    # выселит непрошенное.
    RECS_TRAINER_WARMUP_FILMS: int = Field(default=500, ge=0)

    # --- Расписание ---
    # Сутки. Раздел 5.1 ТЗ: вкусы за ночь не меняются, а суточный цикл даёт
    # запас на повтор при падении внутри двухчасового окна обучения.
    RECS_TRAINER_INTERVAL_SECONDS: int = Field(default=86_400, ge=60)
    # Запускать ли обучение сразу при старте контейнера. На стенде это то, что
    # отличает «поднял стек и увидел рекомендации» от «поднял и ждёшь сутки».
    RECS_TRAINER_RUN_ON_START: bool = True
    RECS_TRAINER_METRICS_PORT: int = 8000
    RECS_TRAINER_METRICS_ENABLED: bool = True
    RECS_TRAINER_DATA_DIR: str = '/var/lib/recsys'

    # --- Генератор синтетики (E2) ---
    # Стенд поднимается с пустым ClickHouse, а без взаимодействий обучать нечего.
    # Раздел 8 ТЗ числит это риском номер один и прямо закладывает генератор.
    RECS_TRAINER_COLLECTOR_URL: str = 'http://analytics-collector:8000'
    RECS_TRAINER_GENERATOR_SEED: int = 42

    # --- Distributed tracing / логи / ошибки ---
    OTEL_ENABLED: bool = True
    OTEL_SERVICE_NAME: str = 'recsys-trainer'
    OTEL_EXPORTER_OTLP_ENDPOINT: str = 'http://jaeger:4318'

    LOG_LEVEL: str = 'INFO'
    LOG_JSON: bool = True

    SENTRY_ENABLED: bool = False
    SENTRY_DSN: str = ''
    SENTRY_ENVIRONMENT: str = 'dev'
    SENTRY_RELEASE: str = ''
    SENTRY_SAMPLE_RATE: float = 1.0
    SENTRY_SEND_DEFAULT_PII: bool = False

    @model_validator(mode='after')
    def _validate_production_hardening(self) -> 'Settings':
        validate_environment(
            self.RECS_TRAINER_ENV,
            key='RECS_TRAINER_ENV',
            insecure_defaults=self.insecure_defaults,
        )
        return self

    @property
    def insecure_defaults(self) -> list[str]:
        """Батч не держит секретов, кроме паролей к базам — их проверяет стенд."""
        return []

    @property
    def shelf_dsn(self) -> str:
        return (
            f'postgresql+psycopg://{self.RECS_POSTGRES_USER}:{self.RECS_POSTGRES_PASSWORD}'
            f'@{self.RECS_POSTGRES_HOST}:{self.RECS_POSTGRES_PORT}/{self.RECS_POSTGRES_DB}'
        )

    @property
    def ratings_dsn(self) -> str:
        return (
            f'postgresql+psycopg://{self.UGC_API_POSTGRES_USER}:{self.UGC_API_POSTGRES_PASSWORD}'
            f'@{self.UGC_API_POSTGRES_HOST}:{self.UGC_API_POSTGRES_PORT}/{self.UGC_API_POSTGRES_DB}'
        )

    @property
    def catalog_dsn(self) -> str:
        return (
            f'postgresql+psycopg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}'
            f'@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}'
        )

    @property
    def clickhouse_endpoints(self) -> list[tuple[str, int]]:
        """``CH_HOSTS`` в пары (host, port). Порт по умолчанию — HTTP-интерфейс."""
        endpoints: list[tuple[str, int]] = []
        for chunk in self.CH_HOSTS.split(','):
            item = chunk.strip()
            if not item:
                continue
            host, _, port = item.partition(':')
            endpoints.append((host, int(port) if port else 8123))
        return endpoints


settings = Settings()
