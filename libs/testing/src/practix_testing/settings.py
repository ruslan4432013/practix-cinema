from pydantic_settings import BaseSettings, SettingsConfigDict


class TestSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    es_host: str = 'http://127.0.0.1:9200'
    es_movies_index: str = 'movies'
    es_persons_index: str = 'persons'
    es_genres_index: str = 'genres'

    redis_host: str = '127.0.0.1'
    redis_port: int = 6379

    service_url: str = 'http://127.0.0.1:8000'


test_settings = TestSettings()
