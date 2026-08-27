"""Конфигурация websocket-шлюза.

Стиль — UPPER_SNAKE_CASE, как в Auth, коллекторе, UGC API и нотификациях.

ПРЕФИКС ``NOTIFY_WS_``, а не ``NOTIFY_``. Голый ``NOTIFY_`` принадлежит сервису
нотификаций — там семьдесят с лишним ключей, и оба контейнера читают ОДИН
корневой ``.env``. Совпадение имени означало бы, что одна переменная управляет
двумя разными процессами.

Три группы имён намеренно БЕЗ своего префикса, потому что описывают не наши
настройки, а чужие сущности, и обязаны совпадать байт в байт у всех, кто их
читает:

* ``AUTHJWT_SECRET_KEY`` — секрет, которым подписывает Auth (имя определяется
  библиотекой ``async-fastapi-jwt-auth``, а не нашим вкусом);
* ``AUTH_REDIS_*`` — экземпляр Redis, в котором Auth ВЕДЁТ денилист;
* ``NOTIFY_AMQP_URL`` — брокер сервиса нотификаций. Своего ``NOTIFY_WS_AMQP_URL``
  нет специально: второй источник правды про адрес брокера рано или поздно
  разъедется с первым, а шлюз подключается ровно к тому же RabbitMQ.

``LOG_*``, ``OTEL_*`` и ``SENTRY_*`` — общие для всего стенда приёмники, сервисы
в них различаются тегом ``service``.
"""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Значение по умолчанию опасно тем, что РАБОТАЕТ: стенд поднимается, ничего не
# ломается, и подмену забывают. В продакшене старт с ним запрещён.
INSECURE_DEFAULT_JWT_SECRET = 'secret'

KNOWN_ENVIRONMENTS = frozenset({'dev', 'test', 'prod'})
DENYLIST_ERROR_POLICIES = frozenset({'raise', 'allow', 'deny'})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    PROJECT_NAME: str = 'notifications-ws'
    NOTIFY_WS_ENV: str = 'dev'

    # --- Брокер ---
    NOTIFY_AMQP_URL: str = 'amqp://guest:guest@rabbitmq:5672/'
    #: Fanout-обменник, в который воркер нотификаций публикует push'и.
    NOTIFY_WS_EXCHANGE: str = 'notifications.ws'
    #: Пауза перед переподключением к брокеру. Шлюз без брокера продолжает
    #: обслуживать соединения — они просто молчат, а клиент видит это по
    #: /health/ready и уходит на ленту.
    NOTIFY_WS_AMQP_RECONNECT_DELAY: float = 5.0

    # --- Ticket'ы для handshake ---
    #: Тридцати секунд хватает браузеру, чтобы открыть сокет сразу после выдачи,
    #: и не хватает, чтобы утёкший из истории/логов ticket кому-то пригодился.
    NOTIFY_WS_TICKET_TTL: int = Field(default=30, ge=5, le=300)
    NOTIFY_WS_TICKET_PREFIX: str = 'ws:ticket:'

    # --- Соединения ---
    #: Вкладок у человека бывает много, но не двадцать. Лимит защищает шлюз от
    #: клиента с циклом переподключения, который не закрывает старые сокеты.
    NOTIFY_WS_MAX_CONNECTIONS_PER_USER: int = Field(default=5, ge=1)
    NOTIFY_WS_MAX_CONNECTIONS: int = Field(default=10_000, ge=1)
    #: Глубина буфера одного соединения. Переполнение — не потеря: клиенту
    #: уходит кадр `desync`, по которому он догоняет ленту.
    NOTIFY_WS_QUEUE_SIZE: int = Field(default=100, ge=1)
    #: Ping от сервера. Балансировщики и прокси рвут «молчащее» соединение через
    #: минуту-две, а поток уведомлений у одного человека сильно реже.
    NOTIFY_WS_PING_INTERVAL: float = Field(default=25.0, ge=1.0)
    #: Токен живёт часами и переживает logout. Перепроверка отзыва — единственное,
    #: что закрывает сокет вышедшего пользователя до истечения срока токена.
    NOTIFY_WS_REVALIDATE_SECONDS: float = Field(default=60.0, ge=5.0)

    # --- Long polling (ступень деградации) ---
    NOTIFY_WS_POLL_TIMEOUT: float = Field(default=25.0, ge=1.0)
    #: Потолок для клиентского ``?wait=``. Больше — и запрос переживёт таймаут
    #: любого промежуточного прокси, а клиент получит обрыв вместо пустого ответа.
    NOTIFY_WS_POLL_MAX_TIMEOUT: float = Field(default=55.0, ge=1.0)
    #: Свой бюджет поллеров, отдельный от сокетного. Вдвое больше, чем сокетов на
    #: человека: брошенный клиентом ``/poll`` живёт на сервере до
    #: ``NOTIFY_WS_POLL_MAX_TIMEOUT`` и штатно пересекается с уже переоткрытым
    #: следующим, так что мгновенных «лишних» запросов у честного клиента больше.
    NOTIFY_WS_MAX_POLLERS_PER_USER: int = Field(default=10, ge=1)
    #: Общий потолок ниже сокетного: поллер дороже — HTTP-запрос плюс задача плюс
    #: очередь на ``NOTIFY_WS_QUEUE_SIZE`` кадров, и всё это в одном воркере.
    NOTIFY_WS_MAX_POLLERS: int = Field(default=2_000, ge=1)

    # --- Политика деградации, которую шлюз ОБЪЯВЛЯЕТ клиенту ---
    # Значения отдаются в ответе на выдачу ticket'а, чтобы поведение фронтенда
    # настраивалось из .env, а не правкой JavaScript.
    NOTIFY_WS_RECONNECT_ATTEMPTS: int = Field(default=3, ge=1)
    NOTIFY_WS_RECONNECT_BASE_DELAY: float = Field(default=1.0, ge=0.1)
    #: Как часто клиент, уехавший на long polling, пробует вернуться на websocket.
    NOTIFY_WS_PROBE_INTERVAL: float = Field(default=60.0, ge=5.0)

    # --- Origin ---
    # У WebSocket НЕТ same-origin policy: браузер откроет сокет к нам с любого
    # сайта, и единственный барьер — проверка Origin на handshake. Список тот же,
    # что у CORS обычных ручек.
    NOTIFY_WS_ALLOWED_ORIGINS: str = 'http://localhost:8090,http://127.0.0.1:8090,http://localhost,http://127.0.0.1'
    #: Клиенты без Origin (websocat, curl, функциональные тесты) — не браузеры,
    #: и защищать их от CSWSH нечего. Выключается на стенде с недоверенной сетью.
    NOTIFY_WS_ALLOW_MISSING_ORIGIN: bool = True

    #: Публичный адрес шлюза, который он отдаёт клиенту вместе с ticket'ом.
    #: Внутри контейнера имени хоста снаружи не видно, а собирать URL на
    #: фронтенде значило бы зашить туда порт.
    NOTIFY_WS_PUBLIC_URL: str = 'ws://localhost:8091'

    # --- Интеграция с Auth (проверка JWT локальная, без сетевых вызовов) ---
    AUTHJWT_SECRET_KEY: str = INSECURE_DEFAULT_JWT_SECRET
    AUTHJWT_DENYLIST_ENABLED: bool = True
    AUTHJWT_DENYLIST_TOKEN_CHECKS: set[str] = {'access'}

    AUTH_REDIS_HOST: str = 'redis'
    AUTH_REDIS_PORT: int = 6379
    AUTH_REDIS_DB: int = 0
    NOTIFY_WS_REDIS_TIMEOUT: float = 1.0
    # 'deny', как в личном кабинете и UGC API (коллектор — 'allow', Auth —
    # 'raise'). Открыть чужому поток уведомлений по токену, про который
    # неизвестно, жив ли он, хуже, чем не открыть его никому.
    NOTIFY_WS_DENYLIST_ON_ERROR: str = 'deny'

    # --- Наблюдаемость ---
    LOG_LEVEL: str = 'INFO'
    LOG_JSON: bool = True
    OTEL_ENABLED: bool = True
    OTEL_SERVICE_NAME: str = 'notifications-ws'
    OTEL_EXPORTER_OTLP_ENDPOINT: str = 'http://jaeger:4318'
    SENTRY_ENABLED: bool = False
    SENTRY_DSN: str = ''
    SENTRY_ENVIRONMENT: str = 'dev'
    SENTRY_RELEASE: str = ''
    SENTRY_SAMPLE_RATE: float = 1.0
    SENTRY_SEND_DEFAULT_PII: bool = False

    @model_validator(mode='after')
    def _validate_production_hardening(self) -> 'Settings':
        normalized = self.NOTIFY_WS_ENV.strip().lower()
        if normalized not in KNOWN_ENVIRONMENTS:
            raise ValueError(f'NOTIFY_WS_ENV must be one of {sorted(KNOWN_ENVIRONMENTS)}, got {self.NOTIFY_WS_ENV!r}')
        if self.NOTIFY_WS_DENYLIST_ON_ERROR not in DENYLIST_ERROR_POLICIES:
            raise ValueError(
                f'NOTIFY_WS_DENYLIST_ON_ERROR must be one of {sorted(DENYLIST_ERROR_POLICIES)}, '
                f'got {self.NOTIFY_WS_DENYLIST_ON_ERROR!r}'
            )
        if self.NOTIFY_WS_POLL_TIMEOUT > self.NOTIFY_WS_POLL_MAX_TIMEOUT:
            raise ValueError('NOTIFY_WS_POLL_TIMEOUT must not exceed NOTIFY_WS_POLL_MAX_TIMEOUT')
        if self.NOTIFY_WS_MAX_POLLERS_PER_USER > self.NOTIFY_WS_MAX_POLLERS:
            raise ValueError('NOTIFY_WS_MAX_POLLERS_PER_USER must not exceed NOTIFY_WS_MAX_POLLERS')
        if normalized == 'prod' and self.insecure_defaults:
            raise ValueError(
                'Insecure default values must be overridden in production: ' + '; '.join(self.insecure_defaults)
            )
        return self

    @property
    def insecure_defaults(self) -> list[str]:
        if self.AUTHJWT_SECRET_KEY == INSECURE_DEFAULT_JWT_SECRET:
            return ['AUTHJWT_SECRET_KEY is the built-in default — anyone can forge an access token']
        return []

    @property
    def allowed_origins(self) -> list[str]:
        return [item.strip() for item in self.NOTIFY_WS_ALLOWED_ORIGINS.split(',') if item.strip()]


settings = Settings()
