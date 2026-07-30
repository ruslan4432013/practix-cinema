import os

from pydantic_settings import BaseSettings, SettingsConfigDict

from practix_movies_api.core.logger import setup_logging

# Логирование настраивается на импорте настроек: этот модуль импортируется
# первым почти в любой цепочке, поэтому к моменту первой записи в лог
# конфигурация уже применена.
setup_logging()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    project_name: str = 'movies'

    es_movies_index: str = 'movies'
    es_persons_index: str = 'persons'
    es_genres_index: str = 'genres'

    redis_host: str = '127.0.0.1'
    redis_port: int = 6379

    elastic_host: str = '127.0.0.1'
    elastic_port: int = 9200
    elastic_schema: str = 'http://'

    base_dir: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    authjwt_secret_key: str = 'secret'
    authjwt_denylist_enabled: bool = True
    authjwt_denylist_token_checks: set = {'access', 'refresh'}

    # --- Auth service integration ---
    # Базовый URL Auth-сервиса для межсервисных запросов (проверка прав).
    auth_api_url: str = 'http://auth:8000'
    # Таймаут запроса к Auth-сервису (сек). Держим маленьким ради изящной деградации.
    auth_request_timeout: float = 2.0
    # Флаг, позволяющий полностью отключить сетевые запросы к Auth-сервису
    # (проверка прав будет опираться только на данные из токена).
    auth_check_enabled: bool = True
    # Порог срабатывания примитивного circuit breaker'а: после стольких подряд
    # неудачных обращений к Auth-сервису запросы к нему временно прекращаются.
    auth_cb_failure_threshold: int = 5
    # Время (сек), на которое «размыкается» circuit breaker после срабатывания.
    auth_cb_reset_seconds: int = 30

    # --- Distributed tracing (OpenTelemetry / Jaeger) ---
    # Флаг включения трассировки. Позволяет отключить экспорт спанов (например,
    # в тестах или локально без запущенного Jaeger).
    otel_enabled: bool = True
    # Имя сервиса, под которым спаны отображаются в Jaeger UI.
    otel_service_name: str = 'movies-api'
    # OTLP/HTTP endpoint коллектора Jaeger (порт 4318).
    otel_exporter_otlp_endpoint: str = 'http://jaeger:4318'


settings = Settings()
