"""Конфигурация сервиса нотификаций.

Стиль именования — UPPER_SNAKE_CASE, как в Auth, коллекторе и UGC API (в Movies
API принят противоположный; см. CLAUDE.md — не смешивать).

ПРЕФИКС ``NOTIFY_``. Все контейнеры стенда читают ОДИН корневой ``.env``, поэтому
префикс обязан быть уникальным: ``UGC_`` уже принадлежит коллектору, ``UGC_API_``
— сервису UGC, ``ETL_``/``CH_`` — конвейеру в ClickHouse. Ключи, общие на весь
стенд (``LOG_*``, ``OTEL_*``, ``SENTRY_*``), намеренно идут БЕЗ префикса: приёмник
у них один на все сервисы, а различает их тег ``service``.

Настройки не вызывают ``setup_logging()`` на импорте — в Auth и Movies API это
сделано и закрывает цикл «логгер → настройки → логгер». Здесь конфигурация
логирования собирается в ``settings.py`` Django, то есть в момент, когда модуль
настроек уже импортирован целиком.
"""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from practix_core.settings import validate_environment

# Значения по умолчанию годятся только для локального стенда. Опасность таких
# значений в том, что они РАБОТАЮТ: сервис поднимается, ничего не ломается, и про
# подмену забывают. Поэтому в продакшене старт с ними запрещён.
INSECURE_DEFAULT_SECRET_KEY = 'change-me-please'
INSECURE_DEFAULT_INTAKE_TOKEN = 'change-me-internal-token'
#: То же значение, что в коллекторе и UGC API: ключ общий на весь стенд.
INSECURE_DEFAULT_JWT_SECRET = 'secret'

DENYLIST_ERROR_POLICIES = frozenset({'raise', 'allow', 'deny'})

# Область действия темпа отправки. Живёт ЗДЕСЬ, а не рядом с самим пейсером
# (`channels/pacing.py`), потому что тот читает настройки — обратный импорт
# замкнул бы цикл.
RATE_SCOPE_GLOBAL = 'global'
RATE_SCOPE_PROCESS = 'process'
SMTP_RATE_SCOPES = frozenset({RATE_SCOPE_GLOBAL, RATE_SCOPE_PROCESS})


def _split_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(',') if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    PROJECT_NAME: str = 'notifications'
    NOTIFY_ENV: str = 'dev'

    # --- Django ---
    NOTIFY_SECRET_KEY: str = INSECURE_DEFAULT_SECRET_KEY
    NOTIFY_DEBUG: bool = False
    NOTIFY_ALLOWED_HOSTS: str = 'localhost,127.0.0.1,[::1],notifications-admin'
    NOTIFY_CSRF_TRUSTED_ORIGINS: str = 'http://localhost:8090,http://127.0.0.1:8090'
    # Базовый адрес, по которому пользователь откроет ссылку отписки из письма.
    NOTIFY_PUBLIC_BASE_URL: str = 'http://localhost:8090'
    # Ссылка отписки не должна протухать быстро: письмо читают и через месяц.
    NOTIFY_UNSUBSCRIBE_TTL_DAYS: int = 365

    # --- Хранилище ---
    NOTIFY_POSTGRES_USER: str = 'postgres'
    NOTIFY_POSTGRES_PASSWORD: str = 'secret'
    NOTIFY_POSTGRES_DB: str = 'notifications_database'
    NOTIFY_POSTGRES_HOST: str = '127.0.0.1'
    NOTIFY_POSTGRES_PORT: int = 5432
    NOTIFY_DB_CONN_MAX_AGE: int = 60

    # --- Брокер ---
    NOTIFY_AMQP_URL: str = 'amqp://guest:guest@rabbitmq:5672/'
    # Heartbeat заметно больше самой долгой блокирующей операции воркера: пока
    # smtplib ждёт ответа почтового сервера, pika не обслуживает соединение, и
    # короткий heartbeat уронил бы канал ровно в момент отправки — а вместе с ним
    # переотправил бы всю пачку.
    NOTIFY_AMQP_HEARTBEAT: int = 600
    NOTIFY_AMQP_BLOCKED_TIMEOUT: int = 300
    # Тайм-ауты ПОДКЛЮЧЕНИЯ. Без них `pika.BlockingConnection` ждёт установления
    # TCP столько, сколько отмерит ядро (минуты на blackhole), а планировщик —
    # один поток, в котором за публикацией стоят тик расписаний, часовые чистки и
    # веер. Самый частый отказ здесь — не «брокер завис», а «брокер
    # перезапускается и не отвечает на SYN», и лечится он именно этими двумя.
    #
    # Чего они НЕ ограничивают — ожидание ответа на уже установленном соединении:
    # это зона heartbeat, и он выше намеренно длинный. Отсюда и размер аренды
    # строки outbox, см. NOTIFY_OUTBOX_CLAIM_TTL_SECONDS.
    #
    # `stack_timeout` — дедлайн всего подъёма TCP/[SSL]/AMQP; pika требует, чтобы
    # он был не меньше `socket_timeout`.
    NOTIFY_AMQP_SOCKET_TIMEOUT: float = Field(default=10.0, gt=0)
    NOTIFY_AMQP_STACK_TIMEOUT: float = Field(default=15.0, gt=0)
    NOTIFY_PREFETCH_COUNT: int = Field(default=1, ge=1)
    NOTIFY_RETRY_TTL_MS: int = 30_000
    NOTIFY_MAX_ATTEMPTS: int = Field(default=5, ge=1)
    # Ярус повтора для СБОРКИ письма отдельный, с большим TTL и большим числом
    # попыток: отправка ждёт почтовый сервер (секунды), сборка — сервис Auth
    # целиком (минуты). 10 попыток по минуте — примерно десять минут терпимого
    # простоя Auth, после которых пачка ложится в dead-letters под глаза дежурному.
    NOTIFY_RETRY_BUILD_TTL_MS: int = 60_000
    NOTIFY_BUILD_MAX_ATTEMPTS: int = Field(default=10, ge=1)

    # --- SMTP ---
    NOTIFY_SMTP_HOST: str = 'mailpit'
    NOTIFY_SMTP_PORT: int = 1025
    NOTIFY_SMTP_USER: str = ''
    NOTIFY_SMTP_PASSWORD: str = ''
    NOTIFY_SMTP_USE_TLS: bool = False
    NOTIFY_SMTP_TIMEOUT: float = 10.0
    NOTIFY_SMTP_FROM: str = 'noreply@practix.local'
    NOTIFY_SMTP_FROM_NAME: str = 'Practix Cinema'
    # Ограничение скорости отправки: почтовый сервер — внешняя система, и уронить
    # его собственной рассылкой значит уронить и себя (теория, «Как посылать
    # быстрее»: Gmail — 500 писем в сутки, Exchange — 30 в минуту).
    #
    # Темп считается НА ВЕСЬ СЕРВИС, а не на процесс: отправляющий воркер
    # масштабируется репликами, и попроцессный лимит давал бы в почтовый сервер
    # `реплики × значение`, то есть отменялся бы добавлением мощности.
    NOTIFY_SMTP_RATE_PER_SECOND: float = Field(default=50.0, gt=0)
    # `global` — общий слот в Redis ядра (том же, где денилист Auth);
    # `process` — прежний попроцессный шаг, без обращений к Redis. Второй режим
    # существует для одиночного стенда и как аварийный выключатель.
    NOTIFY_SMTP_RATE_SCOPE: str = RATE_SCOPE_GLOBAL
    # Потолок ожидания своего слота. В норме недостижим: слот резервируется перед
    # ОДНИМ письмом, поэтому ждущих не больше, чем одновременно отправляющих
    # процессов. Это страховка от абсурдной конфигурации, а не рабочая ручка:
    # превышение отправляет получателя в штатный ярус повторов.
    NOTIFY_SMTP_RATE_MAX_WAIT: float = Field(default=5.0, gt=0)

    # --- Веер и публикация ---
    NOTIFY_FANOUT_DB_BATCH: int = Field(default=500, ge=1)
    NOTIFY_FANOUT_MESSAGE_BATCH: int = Field(default=100, ge=1)
    # Искусственное занижение скорости публикации: пиковый RPS на запись в брокер
    # аффектит соседние очереди того же сервера (см. задание «Только код говорит
    # правду»). Ручка на случай, когда это начнёт мешать.
    NOTIFY_PUBLISH_SLEEP: float = Field(default=0.0, ge=0)
    # Срок годности события. Уведомление, доставленное воркеру спустя сутки после
    # создания, пользователю уже не нужно — теория предлагает дать воркеру право
    # решать, отправлять его или нет.
    NOTIFY_MESSAGE_TTL_HOURS: int = Field(default=24, ge=1)

    # --- Формирующий воркер ---
    # Получателей в одном сообщении «письмо готово». Заметно меньше, чем пачка
    # веера: каждое такое сообщение везёт ГОТОВЫЕ тела писем, и при 100 адресах
    # persistent-сообщение раздувается до мегабайтов.
    NOTIFY_BUILD_MESSAGE_BATCH: int = Field(default=20, ge=1)
    # Идентификаторов в одном запросе к Auth. Верхняя граница совпадает с
    # ограничением самой ручки (`UserLookupRequest.ids`, max_length=500).
    NOTIFY_BUILDER_LOOKUP_BATCH: int = Field(default=100, ge=1, le=500)
    # Ручка нагрузки на Auth, близнец NOTIFY_PUBLISH_SLEEP. Чек-лист теории
    # предлагает регулировать нагрузку на подсистему данных о пользователях двумя
    # способами: числом консьюмеров и таймаутами внутри каждого.
    NOTIFY_BUILDER_SLEEP: float = Field(default=0.0, ge=0)
    # Кеш резолва личности в памяти процесса. 0 выключает. Массовой рассылке он
    # не помогает (каждый адресат встречается в прогоне один раз) — он для
    # событийных и адресных прогонов по одним и тем же людям и для переотправок.
    NOTIFY_DIRECTORY_CACHE_TTL: float = Field(default=300.0, ge=0)
    NOTIFY_DIRECTORY_CACHE_MAX: int = Field(default=10_000, ge=0)

    # --- Планировщик ---
    NOTIFY_SCHEDULER_TICK_SECONDS: float = Field(default=30.0, gt=0)
    NOTIFY_OUTBOX_BATCH: int = Field(default=100, ge=1)
    NOTIFY_SCHEDULE_LOCK_BATCH: int = Field(default=100, ge=1)
    # Сколько раз пробовать опубликовать одну строку outbox, прежде чем перестать
    # ею заниматься. Нужно потому, что «брокер лежит» и «эту конкретную строку
    # некуда доставить» приходят одним исключением: без потолка вторая
    # разновидность встала бы в голову очереди и остановила ВСЕ рассылки.
    NOTIFY_OUTBOX_MAX_ATTEMPTS: int = Field(default=10, ge=1)
    # Аренда захваченной строки. Публикация идёт ВНЕ транзакции, поэтому взаимное
    # исключение между репликами планировщика держит не блокировка строки, а эта
    # отметка: захватив строку, реплика ставит `available_at` в будущее и уходит
    # разговаривать с брокером.
    #
    # ЗНАЧЕНИЕ ПРИВЯЗАНО К HEARTBEAT, а не к тайм-аутам подключения выше. Молча
    # исчезнувшего собеседника pika замечает только по пропущенным heartbeat, то
    # есть `basic_publish` на уже установленном соединении может висеть до двух их
    # интервалов. Аренда короче этого срока истекала бы, пока первая реплика ещё
    # в эфире, — и дубль публикации перестал бы быть аварийным случаем, став
    # штатным. Отношение проверяется на старте (`_validate_publish_timeouts`).
    #
    # Обратная сторона: если планировщика убить по SIGKILL прямо в публикации, его
    # строка не двинется до конца аренды. Это редкий случай (SIGTERM отрабатывает
    # штатно, между итерациями цикла), он виден в `outbox_pending`, и он дешевле
    # регулярных дублей.
    NOTIFY_OUTBOX_CLAIM_TTL_SECONDS: float = Field(default=1800.0, gt=0)
    # Отсрочка после неудачной публикации. Без неё потолок попыток измерялся не в
    # выносливости, а в секундах: слив крутится раз в секунду, и десятисекундная
    # недоступность брокера навсегда выкидывала исправную строку из выборки.
    # С экспонентой те же десять попыток — это уже ~20 минут терпимого отказа.
    NOTIFY_OUTBOX_RETRY_START: float = Field(default=2.0, gt=0)
    NOTIFY_OUTBOX_RETRY_MAX: float = Field(default=300.0, gt=0)
    # Порог, после которого /health/ready говорит degraded: неразобранный outbox
    # означает, что планировщик не работает или брокер недоступен, — сервис
    # отвечает, а рассылки стоят.
    NOTIFY_OUTBOX_ALERT_PENDING: int = Field(default=1000, ge=1)
    # Ретенция журнала попыток. Он растёт вместе с задачами доставки и, в отличие
    # от ленты кабинета, никем не чистился.
    NOTIFY_ATTEMPTS_RETENTION_DAYS: int = Field(default=90, ge=1)
    # Срок жизни отчётного события в очереди `notifications.record-delivery`.
    # Ставится НА СООБЩЕНИИ, а не аргументом очереди: очередь уже объявлена на
    # работающих стендах, и переобъявление с новыми аргументами — 406 и краш-цикл.
    NOTIFY_REPORT_TTL_MS: int = Field(default=86_400_000, ge=1000)

    # --- Тихие часы ---
    # Самая опасная ошибка при рассылке — письмо ночью (теория, «Как испортить
    # жизнь клиенту»). Окно применяется в таймзоне ПОЛУЧАТЕЛЯ, а не сервера.
    NOTIFY_QUIET_HOURS_ENABLED: bool = True
    NOTIFY_QUIET_HOURS_START: str = '22:00'
    NOTIFY_QUIET_HOURS_END: str = '09:00'
    NOTIFY_DEFAULT_TIMEZONE: str = 'Europe/Moscow'

    # --- Шаблонизатор ---
    NOTIFY_TEMPLATE_MAX_BYTES: int = Field(default=65_536, ge=1)
    NOTIFY_RENDER_TIMEOUT_SECONDS: float = Field(default=2.0, gt=0)

    # --- Приём событий извне ---
    NOTIFY_INTAKE_TOKEN: str = INSECURE_DEFAULT_INTAKE_TOKEN
    # Сырой текст в свободном формате валидируется как шаблон, а валидация
    # рендерит его в ОТДЕЛЬНОМ процессе (иначе зациклившийся рендер не прервать).
    # Это процесс на запрос: рубильник существует, чтобы путь можно было закрыть,
    # оставив только `template_id`, который ничего не рендерит на приёме.
    NOTIFY_FREEFORM_RAW_TEXT_ENABLED: bool = True

    # --- Личный кабинет ---
    NOTIFY_INBOX_ENABLED: bool = True
    NOTIFY_INBOX_PREVIEW_CHARS: int = Field(default=512, ge=64)
    # Лента — проекция, а не архив: без срока хранения таблица только растёт.
    NOTIFY_INBOX_RETENTION_DAYS: int = Field(default=180, ge=1)
    NOTIFY_CABINET_PAGE_SIZE: int = Field(default=20, ge=1)
    NOTIFY_CABINET_MAX_PAGE_SIZE: int = Field(default=100, ge=1)
    # Адрес websocket-шлюза для демо-страницы кабинета. Имя без `NOTIFY_WS_`
    # намеренно: тот префикс принадлежит САМОМУ шлюзу, а это настройка страницы,
    # которую отдаёт панель. Значение — публичный адрес снаружи (шлюз опубликован
    # прямым хост-портом, nginx перед ним нет), поэтому имя контейнера не подойдёт.
    NOTIFY_CABINET_WS_URL: str = 'http://localhost:8091'
    # Демо-страница выключается одним ключом: это витрина деградации для учебного
    # стенда, а не часть продуктового кабинета.
    NOTIFY_CABINET_DEMO_ENABLED: bool = True

    # --- Проверка токена пользователя (локально, без сетевых вызовов в Auth) ---
    # Имя ключа — AUTHJWT_SECRET_KEY, а НЕ JWT_SECRET_KEY: в корневом .env есть
    # оба, но подписывает токены async-fastapi-jwt-auth первым из них. UGC API и
    # коллектор читают тот же ключ.
    AUTHJWT_SECRET_KEY: str = INSECURE_DEFAULT_JWT_SECRET
    # Алгоритм по умолчанию у библиотеки — HS256, и Auth его не переопределяет.
    # Ключ префиксный и локальный: стековый AUTHJWT_ALGORITHM, которого Auth не
    # читает, был бы новой ловушкой вида «поменял в одном месте, разъехалось».
    NOTIFY_JWT_ALGORITHM: str = 'HS256'
    NOTIFY_JWT_LEEWAY_SECONDS: float = Field(default=5.0, ge=0)
    NOTIFY_DENYLIST_ENABLED: bool = True
    # 'deny' — закрываемся при недоступности Redis. Прецеденты в репозитории:
    # коллектор 'allow' (потерять аналитику хуже, чем принять лишнее событие),
    # Auth 'raise' (он владеет отзывом), UGC API 'deny'. Здесь отдаётся список
    # тем — о чём человеку писали, какие фильмы и акции; отозванный токен читать
    # это не должен.
    NOTIFY_DENYLIST_ON_ERROR: str = 'deny'
    # Денилист ведёт Auth, поэтому и читается он в ЕГО экземпляре Redis. Свой
    # (или другой номер базы) означал бы, что отозванный токен считается
    # действующим, — ровно та ошибка, которую уже чинили в коллекторе.
    AUTH_REDIS_HOST: str = 'redis'
    AUTH_REDIS_PORT: int = 6379
    AUTH_REDIS_DB: int = 0
    NOTIFY_REDIS_TIMEOUT: float = Field(default=0.5, gt=0)

    # --- Интеграция с Auth ---
    NOTIFY_AUTH_API_URL: str = 'http://auth:8000'
    NOTIFY_AUTH_SERVICE_LOGIN: str = 'svc-notifications'
    NOTIFY_AUTH_SERVICE_PASSWORD: str = 'change-me'
    NOTIFY_AUTH_TIMEOUT: float = 5.0
    NOTIFY_AUTH_MAX_ATTEMPTS: int = Field(default=3, ge=1)
    NOTIFY_SYNC_PAGE_SIZE: int = Field(default=500, ge=1)
    # Потолок на Retry-After, который присылает Auth при 429. Без потолка чужой
    # (или сломанный) заголовок парковал бы воркер на произвольное время.
    NOTIFY_AUTH_RETRY_AFTER_MAX: float = Field(default=60.0, gt=0)

    # --- Интеграция с сервисом сокращения ссылок ---
    # Формирующий воркер выпускает через него ссылку подтверждения адреса, если
    # шаблон письма просит переменную `confirm_url`. Токен общий со стороной
    # шортенера (SHORTENER_INTERNAL_TOKEN) — это одна и та же строка в .env.
    NOTIFY_SHORTENER_API_URL: str = 'http://link-shortener:8000'
    NOTIFY_SHORTENER_INTERNAL_TOKEN: str = 'change-me-shortener-token'
    NOTIFY_SHORTENER_TIMEOUT: float = 3.0
    NOTIFY_SHORTENER_MAX_ATTEMPTS: int = Field(default=3, ge=1)
    # Куда увести ПОСЛЕ успешного подтверждения, если у кампании поле не
    # заполнено. Задание требует главную страницу кинотеатра.
    NOTIFY_CONFIRM_REDIRECT_URL: str = 'http://localhost/'
    # Срок жизни ссылки подтверждения. Отсчитывается от сборки письма, а не от
    # регистрации: письмо, отложенное тихими часами, иначе приезжало бы
    # с уже мёртвой ссылкой.
    NOTIFY_CONFIRM_LINK_TTL_HOURS: int = Field(default=72, ge=1)

    # --- Суперпользователь панели ---
    NOTIFY_SUPERUSER_LOGIN: str = 'admin'
    NOTIFY_SUPERUSER_EMAIL: str = 'admin@example.com'
    NOTIFY_SUPERUSER_PASSWORD: str = 'admin'

    # --- Наблюдаемость (ключи общие на весь стенд, без префикса) ---
    OTEL_ENABLED: bool = True
    OTEL_SERVICE_NAME: str = 'notifications'
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
        if self.NOTIFY_DENYLIST_ON_ERROR not in DENYLIST_ERROR_POLICIES:
            raise ValueError(
                f'NOTIFY_DENYLIST_ON_ERROR must be one of {sorted(DENYLIST_ERROR_POLICIES)}, '
                f'got {self.NOTIFY_DENYLIST_ON_ERROR!r}'
            )
        if self.NOTIFY_SMTP_RATE_SCOPE not in SMTP_RATE_SCOPES:
            # Опечатка здесь молча вернула бы попроцессный лимит, то есть ровно
            # ту ошибку, ради которой область действия и стала настройкой.
            raise ValueError(
                f'NOTIFY_SMTP_RATE_SCOPE must be one of {sorted(SMTP_RATE_SCOPES)}, got {self.NOTIFY_SMTP_RATE_SCOPE!r}'
            )
        validate_environment(self.NOTIFY_ENV, key='NOTIFY_ENV', insecure_defaults=self.insecure_defaults)
        return self

    @model_validator(mode='after')
    def _validate_publish_timeouts(self) -> 'Settings':
        """Тайм-ауты публикации связаны друг с другом, и рассогласование тихое.

        ``stack_timeout`` меньше ``socket_timeout`` pika не принимает вовсе. А
        аренда строки outbox короче двух интервалов heartbeat — это дубль в
        ШТАТНОМ режиме: вторая реплика подхватит строку ровно тогда, когда первая
        ещё висит внутри ``basic_publish``, ожидая молча исчезнувший брокер.
        Ловим на старте, а не по письмам, пришедшим дважды.
        """
        if self.NOTIFY_AMQP_STACK_TIMEOUT < self.NOTIFY_AMQP_SOCKET_TIMEOUT:
            raise ValueError(
                'NOTIFY_AMQP_STACK_TIMEOUT must be >= NOTIFY_AMQP_SOCKET_TIMEOUT '
                f'({self.NOTIFY_AMQP_STACK_TIMEOUT} < {self.NOTIFY_AMQP_SOCKET_TIMEOUT})'
            )
        # Худшее время публикации: молча исчезнувшего собеседника pika замечает
        # только по пропущенным heartbeat.
        publish_deadline = self.NOTIFY_AMQP_HEARTBEAT * 2
        if publish_deadline >= self.NOTIFY_OUTBOX_CLAIM_TTL_SECONDS:
            raise ValueError(
                'NOTIFY_OUTBOX_CLAIM_TTL_SECONDS must exceed 2 * NOTIFY_AMQP_HEARTBEAT '
                f'({self.NOTIFY_OUTBOX_CLAIM_TTL_SECONDS} <= {publish_deadline}): a lease that expires '
                'while the first replica is still inside basic_publish makes a duplicate publish routine'
            )
        return self

    @property
    def insecure_defaults(self) -> list[str]:
        """Секреты, оставшиеся в значении по умолчанию (пишутся в лог при старте)."""
        problems = []
        if self.NOTIFY_SECRET_KEY == INSECURE_DEFAULT_SECRET_KEY:
            problems.append('NOTIFY_SECRET_KEY is the built-in default — session cookies can be forged')
        if self.NOTIFY_INTAKE_TOKEN == INSECURE_DEFAULT_INTAKE_TOKEN:
            problems.append('NOTIFY_INTAKE_TOKEN is the built-in default — anyone can enqueue a mailing')
        if self.AUTHJWT_SECRET_KEY == INSECURE_DEFAULT_JWT_SECRET:
            problems.append('AUTHJWT_SECRET_KEY is the built-in default — anyone can forge a cabinet token')
        if self.NOTIFY_DEBUG:
            problems.append('NOTIFY_DEBUG is on — tracebacks and settings leak to the browser')
        return problems

    @property
    def allowed_hosts(self) -> list[str]:
        return _split_csv(self.NOTIFY_ALLOWED_HOSTS)

    @property
    def csrf_trusted_origins(self) -> list[str]:
        return _split_csv(self.NOTIFY_CSRF_TRUSTED_ORIGINS)


settings = Settings()
