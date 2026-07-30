"""Конфигурация ETL Kafka -> ClickHouse.

Стиль повторяет ``analytics_collector/src/core/config.py``: pydantic-settings,
UPPER_SNAKE, модульный синглтон ``settings``, ``extra='ignore'`` (корневой .env
общий на весь стек и содержит переменные чужих сервисов), у всех полей есть
значения по умолчанию — сервис обязан подниматься с пустым окружением.
"""

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from practix_contracts.v1.topics import ALL_TOPIC_KEYS, DEFAULT_TOPICS


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    PROJECT_NAME: str = 'etl-clickhouse'
    ETL_ENV: str = 'dev'

    # --- Kafka: источник ---------------------------------------------------
    KAFKA_BOOTSTRAP_SERVERS: str = 'kafka-0:9092,kafka-1:9092,kafka-2:9092'
    ETL_CONSUMER_GROUP: str = 'ugc-clickhouse-etl'
    ETL_CLIENT_ID: str = 'etl-clickhouse'
    ETL_AUTO_OFFSET_RESET: str = 'earliest'
    KAFKA_RECONNECT_INTERVAL: float = 5.0

    # Дефолты берутся из контракта: literal-строки здесь были ВТОРОЙ копией
    # шести имён (первая — в коллекторе, третья — в scripts/create_topics.sh).
    # Переопределяются переменными окружения KAFKA_TOPIC_* как и раньше.
    KAFKA_TOPIC_CLICKS: str = DEFAULT_TOPICS['CLICKS']
    KAFKA_TOPIC_PAGE_VIEWS: str = DEFAULT_TOPICS['PAGE_VIEWS']
    KAFKA_TOPIC_VIDEO_EVENTS: str = DEFAULT_TOPICS['VIDEO_EVENTS']
    KAFKA_TOPIC_VIDEO_PROGRESS: str = DEFAULT_TOPICS['VIDEO_PROGRESS']
    KAFKA_TOPIC_SEARCH_EVENTS: str = DEFAULT_TOPICS['SEARCH_EVENTS']
    KAFKA_TOPIC_DLQ: str = DEFAULT_TOPICS['DLQ']

    # --- Батчинг -----------------------------------------------------------
    # Пачка закрывается по любому из трёх порогов. Порог по времени обязателен:
    # без него при слабом трафике события лежали бы в памяти неограниченно
    # долго, и «непрерывный ETL» превратился бы в ночную выгрузку.
    ETL_MAX_RECORDS: int = 5_000
    ETL_BATCH_MAX_ROWS: int = 10_000
    ETL_BATCH_MAX_BYTES: int = 32 * 1024 * 1024
    ETL_FLUSH_INTERVAL: float = 2.0
    ETL_POLL_TIMEOUT_MS: int = 500

    # ГЛАВНЫЙ рычаг потребления памяти — вовсе не ETL_MAX_RECORDS, а буферы
    # выборки aiokafka. Пик RSS ~ fetch_max_bytes + max_partition_fetch_bytes x
    # число назначенных партиций, и уже разобранные в Python-объекты байты
    # весят в 5-10 раз больше самих байт. Регулировать надо этими двумя.
    ETL_MAX_PARTITION_FETCH_BYTES: int = 1 * 1024 * 1024
    ETL_FETCH_MAX_BYTES: int = 16 * 1024 * 1024
    ETL_MAX_POLL_INTERVAL_MS: int = 300_000
    ETL_SESSION_TIMEOUT_MS: int = 30_000

    # Версия конверта, которую ETL умеет разбирать. Событие с большей версией
    # уходит в карантин, а не разбирается молча по устаревшим правилам.
    ETL_MAX_SCHEMA_VERSION: int = 1

    # --- ClickHouse: приёмник ---------------------------------------------
    # По одному узлу на шард: вставка идёт в Distributed-таблицу, которая сама
    # разносит строки. Второй адрес — не реплика, а вход во второй шард, нужен
    # как запасной координатор при недоступности первого.
    CH_HOSTS: str = 'clickhouse-01:8123,clickhouse-03:8123'
    CH_DATABASE: str = 'ugc'
    CH_USER: str = 'etl'
    CH_PASSWORD: str = 'etl'
    CH_SECURE: bool = False
    CH_CONNECT_TIMEOUT: int = 5
    # Единственный работающий таймаут на вставку: async-клиент
    # clickhouse-connect — обёртка синхронного в пул потоков, и asyncio.wait_for
    # вокруг вставки отменил бы корутину, но не HTTP-запрос в потоке.
    CH_SEND_RECEIVE_TIMEOUT: int = 60
    CH_CONNECTOR_LIMIT: int = 8

    # Кворум вставки: ждать подтверждения от обеих реплик шарда — прямой аналог
    # acks=all + min.insync.replicas=2 в Kafka. Отказ реплики превращается в
    # backpressure ETL, а не в окно потери данных. 0 отключает кворум: вставки
    # продолжатся при упавшей реплике ценой этого окна.
    CH_INSERT_QUORUM: int = 2
    CH_HEALTH_INTERVAL: float = 10.0

    # 0 = повторять бесконечно. Это осознанный выбор: отдавать данные некуда,
    # а Kafka хранит их за нас, поэтому «сдаться» означало бы потерять.
    CH_INSERT_MAX_ATTEMPTS: int = 0
    CH_RETRY_START_DELAY: float = 0.5
    CH_RETRY_FACTOR: float = 2.0
    CH_RETRY_MAX_DELAY: float = 30.0

    # --- Наблюдаемость и контроль памяти -----------------------------------
    ETL_METRICS_ENABLED: bool = True
    ETL_METRICS_PORT: int = 8000
    ETL_LAG_INTERVAL: float = 15.0
    ETL_MEMORY_CHECK_INTERVAL: float = 30.0
    ETL_RSS_WARN_MB: int = 512
    # tracemalloc примерно удваивает стоимость каждой аллокации, поэтому по
    # умолчанию выключен. Включать при разборе подозрения на утечку: только он
    # отвечает на вопрос «где именно течёт».
    ETL_TRACEMALLOC_ENABLED: bool = False
    ETL_TRACEMALLOC_INTERVAL: float = 300.0
    ETL_TRACEMALLOC_TOP_N: int = 10

    # --- Распределённая трассировка (OpenTelemetry / Jaeger) ---------------
    # Коллектор кладёт `traceparent` в заголовки сообщений, и без
    # инструментации на этой стороне цепочка «браузер -> коллектор -> Kafka»
    # обрывалась ровно там, где начинается интересное — на пути в хранилище.
    OTEL_ENABLED: bool = True
    OTEL_SERVICE_NAME: str = 'etl-clickhouse'
    OTEL_EXPORTER_OTLP_ENDPOINT: str = 'http://jaeger:4318'
    # Сэмплирование ОБЯЗАТЕЛЬНО, и это не тонкая настройка. Спан на каждое из
    # десятков тысяч событий в секунду сам станет проблемой: экспорт спанов
    # займёт больше ресурсов, чем полезная работа, а Jaeger захлебнётся.
    # 0.01 = один трейс из ста; решение о сэмплировании принимается по
    # trace_id, поэтому оно СОГЛАСОВАНО с решением коллектора — трейс либо
    # целиком попадает в выборку, либо целиком нет, а не рвётся посередине.
    OTEL_TRACES_SAMPLER_RATIO: float = 0.01

    LOG_LEVEL: str = 'INFO'
    LOG_JSON: bool = True

    # --- Сбор ошибок (Sentry / GlitchTip) -----------------------------------
    # Выключено по умолчанию: без поднятого приёмника SDK молотил бы в пустоту.
    # Здесь это особенно ценно: у фонового консьюмера нет ни HTTP-ответа, ни
    # пользователя, который пожалуется, — авария видна только в логах.
    # Сэмплирование ОШИБОК не наследует OTEL_TRACES_SAMPLER_RATIO: события
    # схлопываются приёмником по группам, и терять их незачем.
    SENTRY_ENABLED: bool = False
    SENTRY_DSN: str = ''
    SENTRY_ENVIRONMENT: str = 'dev'
    SENTRY_RELEASE: str = ''
    SENTRY_SAMPLE_RATE: float = 1.0
    SENTRY_SEND_DEFAULT_PII: bool = False

    @field_validator('ETL_ENV')
    @classmethod
    def _normalize_env(cls, value: str) -> str:
        return value.strip().lower()

    @model_validator(mode='after')
    def _validate_production_hardening(self) -> 'Settings':
        """Не даём подняться в продакшене с настройками из примера.

        Падение на старте здесь лучше тихой работы: ETL с дефолтным паролем
        ходит в хранилище, где лежат все пользовательские данные аналитики.
        """
        if self.ETL_ENV in {'prod', 'production'} and self.CH_PASSWORD == 'etl':
            raise ValueError('CH_PASSWORD must be changed from the default value in production')
        return self

    @property
    def kafka_bootstrap_list(self) -> list[str]:
        return [item.strip() for item in self.KAFKA_BOOTSTRAP_SERVERS.split(',') if item.strip()]

    @property
    def clickhouse_hosts(self) -> list[tuple[str, int]]:
        """Список (host, port) приёмника. Порт по умолчанию — HTTP 8123."""
        hosts: list[tuple[str, int]] = []
        for item in self.CH_HOSTS.split(','):
            item = item.strip()
            if not item:
                continue
            host, _, port = item.partition(':')
            hosts.append((host, int(port) if port else 8123))
        return hosts

    @property
    def topics(self) -> list[str]:
        """Топики, которые читает ETL, в каноническом порядке из контракта.

        DLQ включён намеренно: события, не доехавшие с первой попытки, — такие же
        данные, и терять их в хранилище было бы странно. Исходный топик такого
        сообщения виден в заголовке x-original-topic.

        Порядок и состав раньше дублировались здесь и в brokers/topics.py
        коллектора: добавление топика требовало двух согласованных правок, а
        пропуск одной означал бы, что ETL не читает то, что коллектор уже пишет.
        """
        return [getattr(self, f'KAFKA_TOPIC_{key}') for key in ALL_TOPIC_KEYS]


settings = Settings()


if __name__ == '__main__':
    print(settings.model_dump())  # noqa: T201 — отладочный вывод конфигурации
