"""Конфигурация сервиса выдачи рекомендаций.

Стиль именования — UPPER_SNAKE_CASE, как в Auth, коллекторе, UGC, нотификациях
и шортенере (в Movies API принят противоположный; см. CLAUDE.md — не смешивать).

ПРЕФИКС ``RECS_``. Все контейнеры читают ОДИН корневой ``.env``, поэтому
префикс обязан быть свободным: ``UGC_`` занят коллектором, ``UGC_API_`` — UGC,
``NOTIFY_``/``NOTIFY_WS_`` — рассылками и шлюзом, ``SHORTENER_`` — короткими
ссылками, ``ETL_``/``CH_`` — ETL и клиентом ClickHouse. ``RECS_`` не занят ничем.

``RECS_POSTGRES_*`` и ``RECS_REDIS_*`` объявлены ТАКИМИ ЖЕ именами в
``recsys-trainer``: витрина одна, и связность к ней обязана описываться одним
набором ключей. Второй набор на то же соединение — это ровно тот способ,
которым конфигурация расходится с реальностью.

Логгер отсюда не вызывается: в Auth и Movies API ``core/config.py`` дёргает
``setup_logging()`` на импорте, и из-за этого настройки нельзя импортировать
внутри логгера — цикл. Здесь логирование настраивается из ``main.py``.
"""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from practix_core.settings import validate_environment

# Значение по умолчанию годится только для локальной разработки и тестов.
# Опасность таких значений в том, что они работают: сервис поднимается, ничего
# не ломается, и подмену забывают. Поэтому в продакшене старт с ним запрещён.
INSECURE_DEFAULT_JWT_SECRET = 'secret'


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    PROJECT_NAME: str = 'recommendations-api'
    RECS_ENV: str = 'dev'

    # --- Витрина: источник истины ---
    RECS_POSTGRES_USER: str = 'postgres'
    RECS_POSTGRES_PASSWORD: str = 'secret'
    RECS_POSTGRES_DB: str = 'recs_database'
    RECS_POSTGRES_HOST: str = '127.0.0.1'
    RECS_POSTGRES_PORT: int = 5432
    RECS_DB_ECHO: bool = False
    # ПОТОЛОК ПУЛА СЧИТАЕТСЯ НА КОНТЕЙНЕР, А НЕ НА ПРОЦЕСС. Пул принадлежит
    # воркеру uvicorn, а воркеров четыре (см. CMD таргета recommendations-api в
    # infra/docker/python-service.Dockerfile), поэтому база видит вчетверо
    # больше соединений, чем написано здесь: 4 × (POOL_SIZE + MAX_OVERFLOW).
    # Прежние 10 + 20 давали 120 при `max_connections` = 100 у recs-db — то
    # есть под нагрузкой запросы получали бы `too many connections` ровно
    # тогда, когда витрина нужнее всего, и ступень деградации отработала бы на
    # отказе, которого никто не заказывал.
    #
    # 5 + 5 держат бюджет recs-db с запасом (арифметика — в комментарии к
    # recs-db в infra/compose/docker-compose.yml, а её нарушение ловит
    # apps/recommendations-api/tests/unit/test_connection_budget.py):
    #   выдача 4 × 10 = 40, батч ≤ 16, миграции ≤ 2 — против 97 доступных.
    # Про запас по производительности: витрина не горячий путь (первым идёт
    # Redis), плановая нагрузка — 100 RPS, замеренный p95 — 46 мс, так что
    # десяти соединений на воркер хватает с многократным избытком.
    RECS_DB_POOL_SIZE: int = 5
    RECS_DB_MAX_OVERFLOW: int = 5
    # ОБЯЗАТЕЛЬНЫЙ ТАЙМАУТ, а не тюнинг. Лестница деградации отдаёт популярное,
    # когда витрина недоступна, — но «недоступна» обязана обнаруживаться БЫСТРО.
    # Остановленный контейнер базы не отвечает отказом: его адрес просто
    # исчезает из сети, и TCP-подключение висит до системного таймаута. Запрос
    # при этом не падает и не деградирует — он ждёт, nginx обрывает его по
    # proxy_read_timeout, и пользователь получает 504 вместо 200 с популярным.
    # То есть вся ступень лестницы не срабатывает ровно в том сценарии, ради
    # которого она написана.
    #
    # Значение заведомо меньше proxy_read_timeout (3s) в infra/nginx/configs/site.conf.
    RECS_DB_TIMEOUT: float = Field(default=2.0, gt=0.0)

    # --- Витрина: горячий слой ---
    # Отдельный экземпляр Redis, а не база в общем: витрина живёт по своим
    # правилам вытеснения (volatile-lru + TTL на ключах версии), и делить их с
    # сессиями Auth значило бы связать судьбу входа на сайт с судьбой блока
    # рекомендаций.
    RECS_REDIS_HOST: str = '127.0.0.1'
    RECS_REDIS_PORT: int = 6379
    RECS_REDIS_DB: int = 0
    # Полсекунды: чтение кэша, занявшее больше, уже не ускоряет ответ, а клиент
    # redis-py умножает этот таймаут на число повторов подключения — из чего и
    # складывался перерасход бюджета на погашенном контейнере.
    RECS_REDIS_TIMEOUT: float = 0.5
    # TTL ключей версии. Именно он убирает старую версию батча после
    # переключения указателя — массового удаления ключей нет (ADR-004: удаление
    # между старой и новой витриной само себе создаёт пик промахов).
    # Заведомо больше суточного цикла обучения, иначе горячий слой опустеет
    # раньше, чем придёт следующая версия.
    RECS_CACHE_TTL: int = Field(default=172_800, ge=60)
    # Сколько держать в памяти процесса номер актуальной версии, прежде чем
    # перечитать указатель. Секунда — компромисс: за это время переключение
    # версии доезжает до всех воркеров, а походов в Redis на каждый запрос нет.
    RECS_POINTER_TTL: float = Field(default=1.0, ge=0.0)
    # Предохранитель горячего слоя: после первой неудачи Redis не трогаем
    # столько секунд. Погашенный контейнер не отказывает, а молчит, и без
    # предохранителя КАЖДЫЙ запрос платил бы два таймаута — вдвоём они съедали
    # бюджет раньше, чем очередь доходила до PostgreSQL, и отказ ускорителя
    # отменял работу источника истины. Тот же приём, что в пейсинге SMTP у
    # нотификаций.
    RECS_CACHE_FUSE_SECONDS: float = Field(default=5.0, ge=0.0)

    # --- Бюджет времени на ответ ---
    # Жёсткий срок на весь обработчик, заведомо меньший proxy_read_timeout (3s)
    # маршрута ^~ /api/v1/recommendations. Нужен потому, что отказ источника
    # бывает не только «ошибкой», но и «молчанием»: ожидания Redis и PostgreSQL
    # складываются, и без общего срока запрос перекрывает таймаут nginx —
    # пользователь получает 504 вместо 200 с популярным. См. services/degradation.py.
    RECS_REQUEST_BUDGET: float = Field(default=2.5, gt=0.0)

    # --- Форма выдачи ---
    RECS_DEFAULT_LIMIT: int = Field(default=10, ge=1, le=100)
    RECS_MAX_LIMIT: int = Field(default=50, ge=1, le=200)

    # --- Обогащение карточек из Movies API (необязательное) ---
    # По умолчанию ВЫКЛЮЧЕНО. Каталог не дублируется в рекомендациях: у него уже
    # есть владелец, и горячий путь отдаёт идентификаторы, а карточки берёт
    # фронт. Флаг ?enrich=true существует, чтобы стык был проверяем, а не чтобы
    # им пользовались на 100 RPS: N карточек — это N запросов к Movies API,
    # bulk-ручки по списку идентификаторов у него нет.
    RECS_ENRICH_ENABLED: bool = True
    RECS_ENRICH_MAX: int = Field(default=20, ge=1, le=50)
    RECS_MOVIES_API_URL: str = 'http://api:8000'
    RECS_MOVIES_API_TIMEOUT: float = 1.0

    # --- Интеграция с Auth (проверка JWT локальная, без сетевых вызовов) ---
    AUTHJWT_SECRET_KEY: str = INSECURE_DEFAULT_JWT_SECRET
    AUTHJWT_DENYLIST_ENABLED: bool = True
    AUTHJWT_DENYLIST_TOKEN_CHECKS: set[str] = {'access'}

    # Денилист отозванных токенов ведёт Auth, и читать его надо там же, где он
    # пишется, — в Redis Auth-сервиса (база 0). Это НЕ RECS_REDIS_*: там витрина.
    REDIS_HOST: str = '127.0.0.1'
    REDIS_PORT: int = 6379
    AUTH_REDIS_HOST: str | None = None
    AUTH_REDIS_PORT: int | None = None
    AUTH_REDIS_DB: int = 0
    RECS_AUTH_REDIS_TIMEOUT: float = 1.0

    # --- Distributed tracing (OpenTelemetry / Jaeger) ---
    OTEL_ENABLED: bool = True
    OTEL_SERVICE_NAME: str = 'recommendations-api'
    OTEL_EXPORTER_OTLP_ENDPOINT: str = 'http://jaeger:4318'

    LOG_LEVEL: str = 'INFO'
    LOG_JSON: bool = True

    RECS_METRICS_ENABLED: bool = True

    # --- Сбор ошибок (Sentry / GlitchTip) ---
    # Префикс БЕЗ RECS_: ключи общие на весь стенд, один приёмник на все
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
        validate_environment(self.RECS_ENV, key='RECS_ENV', insecure_defaults=self.insecure_defaults)
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
            f'postgresql+asyncpg://{self.RECS_POSTGRES_USER}:{self.RECS_POSTGRES_PASSWORD}'
            f'@{self.RECS_POSTGRES_HOST}:{self.RECS_POSTGRES_PORT}/{self.RECS_POSTGRES_DB}'
        )

    @property
    def auth_redis_host(self) -> str:
        return self.AUTH_REDIS_HOST or self.REDIS_HOST

    @property
    def auth_redis_port(self) -> int:
        return self.AUTH_REDIS_PORT or self.REDIS_PORT

    def clamp_limit(self, limit: int | None) -> int:
        """Сколько позиций отдавать: значение по умолчанию и жёсткий потолок."""
        if limit is None:
            return self.RECS_DEFAULT_LIMIT
        return max(1, min(limit, self.RECS_MAX_LIMIT))


settings = Settings()
