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

    model_config = SettingsConfigDict(env_file=env_path, env_file_encoding='utf-8', extra='ignore')


settings = Settings()

if __name__ == '__main__':
    print(settings.model_dump())  # noqa: T201 — отладочный вывод конфигурации
