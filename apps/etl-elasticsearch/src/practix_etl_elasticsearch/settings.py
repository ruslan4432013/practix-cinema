from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

current_file = Path(__file__).resolve()

current_dir = current_file.parent

env_path = current_dir.parent / '.env'


class Settings(BaseSettings):
    POSTGRES_USER: str
    POSTGRES_PASSWORD: str
    POSTGRES_DB: str
    POSTGRES_PORT: int
    POSTGRES_HOST: str

    ELASTICSEARCH_HOST: str = '127.0.0.1'
    ELASTICSEARCH_PORT: int = 9200
    ELASTICSEARCH_INDEX: str = 'movies'
    ELASTICSEARCH_GENRES_INDEX: str = 'genres'
    ELASTICSEARCH_PERSONS_INDEX: str = 'persons'

    BATCH_SIZE: int = 100
    STATE_FILE_PATH: str = str(current_dir / 'etl_state.json')
    ETL_SLEEP_SECONDS: float = 10.0

    # Трассировки у этого сервиса нет, но имя всё равно нужно: в репозитории
    # OTEL_SERVICE_NAME — каноническое имя сервиса, под ним он и подписывается
    # в сборщике ошибок (PROJECT_NAME общий на весь стенд и для этого не годится).
    OTEL_SERVICE_NAME: str = 'etl-elasticsearch'

    # --- Логи ---
    # Те же ключи, что у остальных сервисов стенда (они уже есть в корневом
    # .env): JSON собирается сборщиком в ELK, текст удобнее при локальной
    # отладке.
    LOG_LEVEL: str = 'INFO'
    LOG_JSON: bool = True

    # --- Сбор ошибок (Sentry / GlitchTip) ---
    # Выключено по умолчанию: без поднятого приёмника SDK молотил бы в пустоту.
    # Для этого сервиса сбор ошибок особенно уместен: цикл в main.py гасит
    # исключения через `except Exception: logger.exception(...)`, и до сих пор
    # единственным следом аварии была строка в stdout контейнера.
    SENTRY_ENABLED: bool = False
    SENTRY_DSN: str = ''
    SENTRY_ENVIRONMENT: str = 'dev'
    SENTRY_RELEASE: str = ''
    SENTRY_SAMPLE_RATE: float = 1.0
    SENTRY_SEND_DEFAULT_PII: bool = False

    model_config = SettingsConfigDict(env_file=env_path, env_file_encoding='utf-8', extra='ignore')


settings = Settings()

if __name__ == '__main__':
    print(settings.model_dump())  # noqa: T201 — отладочный вывод конфигурации
