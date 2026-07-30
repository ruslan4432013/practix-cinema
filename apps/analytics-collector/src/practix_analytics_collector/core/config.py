"""Конфигурация сервиса сбора пользовательских действий.

Стиль именования полей — UPPER_SNAKE_CASE, как в Auth-сервисе (в Movies API
принят противоположный, lower_snake_case; см. CLAUDE.md — не смешивать).

Все поля имеют значения по умолчанию: сервис сбора аналитики обязан подниматься
даже с неполным окружением, иначе ошибка конфигурации превращается в отказ
приёма событий. Исключение — секреты (соль хеширования IP и ключ подписи JWT):
в продакшене старт с их значениями по умолчанию запрещён, вне продакшена сервис
громко предупреждает о них в логе при старте.
"""

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from practix_contracts.v1.topics import DEFAULT_TOPICS
from practix_core.net import Network, parse_networks

# Значения по умолчанию, пригодные только для локальной разработки и тестов;
# в продакшене (UGC_ENV=prod) старт с ними запрещён, вне продакшена — сервис
# громко предупреждает о них при старте.
INSECURE_DEFAULT_SALT = 'dev-insecure-salt-change-me'
INSECURE_DEFAULT_JWT_SECRET = 'secret'

# Допустимые окружения. Неизвестное значение — ошибка старта, см. _normalize_env.
KNOWN_ENVIRONMENTS = frozenset({'dev', 'test', 'prod'})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    PROJECT_NAME: str = 'analytics-collector'
    # Окружение: 'dev' | 'test' | 'prod'. Влияет на строгость проверок на старте.
    UGC_ENV: str = 'dev'

    # --- Redis ---
    # Общий адрес по умолчанию; конкретные подсистемы ниже могут его перекрыть.
    REDIS_HOST: str = '127.0.0.1'
    REDIS_PORT: int = 6379

    # Собственные данные коллектора: буфер деградации, счётчики rate limit,
    # дедупликация. Живут в ОТДЕЛЬНОМ экземпляре Redis (compose: redis-ugc).
    # Разделения по номеру базы было недостаточно: базы делят память и судьбу,
    # поэтому многочасовой отказ Kafka вытеснял бы сессии Auth — авария в
    # аналитике роняла бы вход на сайт.
    UGC_REDIS_HOST: str | None = None
    UGC_REDIS_PORT: int | None = None
    UGC_REDIS_DB: int = 1
    UGC_REDIS_MAX_CONNECTIONS: int = 100
    # Таймаут операций Redis. Держим маленьким: Redis здесь — путь деградации,
    # он не должен сам стать источником задержек на горячем пути.
    UGC_REDIS_TIMEOUT: float = 0.5

    # Денилист отозванных токенов ведёт Auth-сервис, и читать его надо ТАМ, ЖЕ
    # ГДЕ ОН ПИШЕТСЯ. Отдельные поля появились вместе с разнесением экземпляров:
    # до этого проверка ходила в базу коллектора (UGC_REDIS_DB=1), где ключей
    # денилиста нет и никогда не было, то есть отозванный токен здесь считался
    # действительным.
    AUTH_REDIS_HOST: str | None = None
    AUTH_REDIS_PORT: int | None = None
    AUTH_REDIS_DB: int = 0

    # --- Kafka ---
    KAFKA_BOOTSTRAP_SERVERS: str = 'kafka-0:9092,kafka-1:9092,kafka-2:9092'
    KAFKA_CLIENT_ID: str = 'analytics-collector'
    # acks=all + идемпотентный продюсер — запись подтверждается только после
    # реплицирования на min.insync.replicas узлов (см. docs/kafka_topics.md).
    KAFKA_ACKS: str = 'all'
    KAFKA_ENABLE_IDEMPOTENCE: bool = True
    KAFKA_COMPRESSION_TYPE: str = 'lz4'
    # Микро-батчинг: продюсер ждёт до 20 мс, набирая записи в один запрос.
    # Заметно снижает число round-trip'ов к брокеру при высоком RPS.
    KAFKA_LINGER_MS: int = 20
    KAFKA_MAX_BATCH_SIZE: int = 64 * 1024
    KAFKA_REQUEST_TIMEOUT_MS: int = 15_000
    # Сколько ждать место в буфере продюсера, прежде чем признать Kafka недоступной.
    # Маленькое значение — намеренно: при вставшем брокере запрос должен быстро
    # уйти в fallback, а не копить корутины.
    KAFKA_MAX_BLOCK_MS: int = 1_000
    # Периодичность попыток переподключения, когда продюсер в degraded-режиме.
    KAFKA_RECONNECT_INTERVAL: float = 5.0

    # --- Имена топиков (резолвятся только на сервере, никогда не из запроса) ---
    # Дефолты берутся из контракта: literal-строки здесь были ВТОРОЙ копией
    # шести имён (первая — в коллекторе, третья — в scripts/create_topics.sh).
    # Переопределяются переменными окружения KAFKA_TOPIC_* как и раньше.
    KAFKA_TOPIC_CLICKS: str = DEFAULT_TOPICS['CLICKS']
    KAFKA_TOPIC_PAGE_VIEWS: str = DEFAULT_TOPICS['PAGE_VIEWS']
    KAFKA_TOPIC_VIDEO_EVENTS: str = DEFAULT_TOPICS['VIDEO_EVENTS']
    KAFKA_TOPIC_VIDEO_PROGRESS: str = DEFAULT_TOPICS['VIDEO_PROGRESS']
    KAFKA_TOPIC_SEARCH_EVENTS: str = DEFAULT_TOPICS['SEARCH_EVENTS']
    KAFKA_TOPIC_DLQ: str = DEFAULT_TOPICS['DLQ']

    # --- Буфер деградации (Kafka недоступна) ---
    UGC_FALLBACK_ENABLED: bool = True
    UGC_FALLBACK_KEY: str = 'ugc:fallback:events'
    # Верхняя граница длины буфера. Ограничение обязательно: без него отказ Kafka
    # приводит к неограниченному росту памяти Redis.
    UGC_FALLBACK_MAX_SIZE: int = 100_000
    UGC_FALLBACK_DRAIN_INTERVAL: float = 5.0
    UGC_FALLBACK_DRAIN_BATCH: int = 500
    # После стольких неудачных попыток доставки запись уходит в DLQ-топик.
    UGC_FALLBACK_MAX_ATTEMPTS: int = 5

    # --- Ограничения полезной нагрузки (защита от abuse) ---
    UGC_MAX_BODY_BYTES: int = 64 * 1024
    UGC_MAX_BATCH_SIZE: int = 50

    # --- Дедупликация по event_id ---
    UGC_DEDUP_ENABLED: bool = True
    UGC_DEDUP_TTL: int = 600

    # --- Rate limiting ---
    UGC_RATE_LIMIT_ENABLED: bool = True
    # Лимит существенно выше, чем в Auth: это ingest-ручка, браузер активного
    # пользователя штатно шлёт десятки событий в минуту.
    UGC_RATE_LIMIT_TIMES: int = 600
    UGC_RATE_LIMIT_SECONDS: int = 60
    # Лимит по идентификатору браузера и сессии В ДОПОЛНЕНИЕ к лимиту по IP.
    # Одного IP недостаточно с обеих сторон: он слаб против ботнета (тысяча
    # адресов — тысяча лимитов) и, наоборот, бьёт по легитимным пользователям
    # за общим NAT — корпоративной сетью или мобильным оператором, где под
    # одним адресом сидят тысячи человек. Идентификатор браузера разделяет их.
    UGC_RATE_LIMIT_BY_IDENTITY: bool = True
    # Порог на один браузер: заметно ниже общего лимита по IP, потому что за
    # IP может честно стоять много клиентов, а за anonymous_id — один.
    UGC_IDENTITY_RATE_LIMIT_TIMES: int = 300

    # --- Приватность ---
    # Соль для необратимого хеширования IP. Сырой IP не покидает процесс.
    UGC_IP_HASH_SALT: str = INSECURE_DEFAULT_SALT
    UGC_MAX_USER_AGENT_LENGTH: int = 256
    # Сети, из которых принимается X-Forwarded-For / X-Real-IP. По умолчанию —
    # диапазоны Docker и приватные сети RFC1918, то есть Nginx из compose.
    # Пустая строка отключает проверку (нужно тестам, где приложение поднято
    # in-process и адреса соединения нет).
    UGC_TRUSTED_PROXIES: str = '172.16.0.0/12,192.168.0.0/16,10.0.0.0/8,127.0.0.0/8'

    # --- Фильтрация ботов ---
    # События с device_type == bot отбрасываются ДО публикации. Раньше они
    # попадали в топик и засоряли статистику: краулер, обходящий каталог, даёт
    # десятки тысяч «просмотров страниц», которые аналитику приходится
    # вычищать уже в хранилище — то есть платить за их приём, передачу и
    # хранение, чтобы затем от них избавиться.
    UGC_DROP_BOT_EVENTS: bool = True

    # --- CORS ---
    # Список источников через запятую. '*' допустим только вне продакшена.
    UGC_CORS_ALLOW_ORIGINS: str = 'http://localhost,http://127.0.0.1'

    # --- Интеграция с Auth (проверка JWT выполняется локально, без сетевых вызовов) ---
    AUTHJWT_SECRET_KEY: str = INSECURE_DEFAULT_JWT_SECRET
    AUTHJWT_DENYLIST_ENABLED: bool = True
    AUTHJWT_DENYLIST_TOKEN_CHECKS: set[str] = {'access'}

    # --- Метрики ---
    UGC_METRICS_ENABLED: bool = True

    # --- Distributed tracing (OpenTelemetry / Jaeger) ---
    OTEL_ENABLED: bool = True
    OTEL_SERVICE_NAME: str = 'analytics-collector'
    OTEL_EXPORTER_OTLP_ENDPOINT: str = 'http://jaeger:4318'

    LOG_LEVEL: str = 'INFO'
    # JSON-логи удобны для сбора в ELK/Loki; в локальной разработке читаемее текст.
    LOG_JSON: bool = True

    @field_validator('UGC_ENV')
    @classmethod
    def _normalize_env(cls, value: str) -> str:
        """Нормализует и ПРОВЕРЯЕТ имя окружения.

        Проверка на список значений здесь не педантизм. Раньше все строгие
        проверки включались условием ``UGC_ENV == 'prod'``, поэтому опечатка
        (``production``, ``PROD ``, пустая строка) тихо переводила сервис в
        мягкий режим — ровно тот сценарий, ради которого проверки и вводились.
        Теперь неизвестное окружение — ошибка старта.
        """
        normalized = value.strip().lower()
        if normalized not in KNOWN_ENVIRONMENTS:
            raise ValueError(f'UGC_ENV must be one of {sorted(KNOWN_ENVIRONMENTS)}, got {value!r}')
        return normalized

    @model_validator(mode='after')
    def _validate_production_hardening(self) -> 'Settings':
        """Не даём подняться в продакшене с небезопасными настройками.

        Падение на старте здесь — сознательный выбор: тихо собирать события с
        предсказуемым хешем IP или с открытым CORS хуже, чем не стартовать вовсе.
        """
        if self.UGC_ENV != 'prod':
            return self
        if self.insecure_defaults:
            raise ValueError(
                'Insecure default values must be overridden in production: ' + '; '.join(self.insecure_defaults)
            )
        if '*' in self.cors_origins:
            raise ValueError('UGC_CORS_ALLOW_ORIGINS must not be "*" in production')
        return self

    @property
    def insecure_defaults(self) -> list[str]:
        """Секреты, оставшиеся в значении по умолчанию.

        В продакшене этот список обязан быть пустым (иначе сервис не стартует),
        вне продакшена — выводится предупреждением при старте (см. main.py).
        Опасность значений по умолчанию именно в том, что они работают, поэтому
        их отсутствие должно быть заметным событием, а не тишиной.
        """
        problems = []
        if self.UGC_IP_HASH_SALT == INSECURE_DEFAULT_SALT:
            problems.append('UGC_IP_HASH_SALT is the built-in default — IP hashes are reversible via a rainbow table')
        if self.AUTHJWT_SECRET_KEY == INSECURE_DEFAULT_JWT_SECRET:
            problems.append('AUTHJWT_SECRET_KEY is the built-in default — anyone can forge an access token')
        return problems

    @property
    def trusted_proxy_networks(self) -> list[Network]:
        """Сети, из которых принимаются заголовки с адресом клиента.

        Разбор и обоснование строгости — в ``practix_core.net.parse_networks``.
        """
        return parse_networks(self.UGC_TRUSTED_PROXIES)

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.UGC_CORS_ALLOW_ORIGINS.split(',') if origin.strip()]

    @property
    def kafka_bootstrap_list(self) -> list[str]:
        return [server.strip() for server in self.KAFKA_BOOTSTRAP_SERVERS.split(',') if server.strip()]

    @property
    def ugc_redis_host(self) -> str:
        return self.UGC_REDIS_HOST or self.REDIS_HOST

    @property
    def ugc_redis_port(self) -> int:
        return self.UGC_REDIS_PORT or self.REDIS_PORT

    @property
    def auth_redis_host(self) -> str:
        return self.AUTH_REDIS_HOST or self.REDIS_HOST

    @property
    def auth_redis_port(self) -> int:
        return self.AUTH_REDIS_PORT or self.REDIS_PORT

    @property
    def redis_url(self) -> str:
        return f'redis://{self.ugc_redis_host}:{self.ugc_redis_port}/{self.UGC_REDIS_DB}'


settings = Settings()
