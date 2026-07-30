-- Сырой поток всех семейств событий.
--
-- Единая широкая таблица, а не таблица на тип события: конверт у всех событий
-- одинаковый, а специфика лежит в payload. Аналитик получает одну точку входа
-- для любых кросс-продуктовых срезов («что делал пользователь до того, как
-- бросил фильм»), а ETL — один код вставки вместо шести.

CREATE TABLE IF NOT EXISTS ugc.raw_events_local ON CLUSTER '{cluster}'
(
    -- --- Конверт EventEnvelope (analytics_collector/src/models/events.py) ---
    event_id          UUID,
    event_type        LowCardinality(String),
    schema_version    UInt16,
    -- Время по часам клиента: может быть сбито или подделано, поэтому Nullable
    -- и никогда не используется как единственный источник времени.
    event_timestamp   Nullable(DateTime64(3, 'UTC')),
    -- Серверное время приёма — авторитетное, по нему партиционируем.
    received_at       DateTime64(3, 'UTC'),
    user_id           Nullable(UUID),
    is_authenticated  UInt8,
    anonymous_id      String,
    session_id        String,
    -- Ключ, по которому коллектор партиционировал сообщение в Kafka
    -- (user_id -> anonymous_id -> session_id). Он же ключ шардирования:
    -- одна партиция Kafka -> один шард ClickHouse.
    partition_key     String,

    -- --- EventContext ---
    url               String,
    referrer          String,
    screen_width      UInt16,
    screen_height     UInt16,
    viewport_width    UInt16,
    viewport_height   UInt16,
    locale            LowCardinality(String),
    timezone          LowCardinality(String),
    user_agent        String,
    device_type       LowCardinality(String),
    os                LowCardinality(String),
    browser           LowCardinality(String),
    -- Не сам IP, а его соль-хеш: коллектор не пишет сырой адрес никуда.
    ip_hash           String,

    -- Специфика типа события — как есть, байт в байт. Хранить сырой JSON
    -- обязательно: поле, которое сегодня никем не разобрано, завтра
    -- понадобится аналитику, а перечитать Kafka через месяц уже нельзя —
    -- retention там 7-30 дней.
    payload           String CODEC(ZSTD(3)),

    -- --- Происхождение записи ---
    -- Нужно для разбора инцидентов и для сверки «сколько прочитали из топика»
    -- против «сколько легло в таблицу».
    kafka_topic       LowCardinality(String),
    -- Из заголовка x-original-topic: у сообщений, приехавших через DLQ,
    -- kafka_topic — это DLQ, а исходный топик виден только здесь.
    origin_topic      LowCardinality(String),
    kafka_partition   UInt16,
    kafka_offset      UInt64,
    ingested_at       DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC')
)
-- ReplacingMergeTree по версии ingested_at — третий (последний) уровень
-- дедупликации: доставка из Kafka гарантирована «хотя бы один раз», поэтому
-- дубликаты будут, и гасить их обязан приёмник.
ENGINE = ReplicatedReplacingMergeTree(ingested_at)
PARTITION BY toYYYYMM(received_at)
-- ORDER BY = ключ дедупликации. Все три поля стабильны у копий одного события
-- (их проставил коллектор один раз), поэтому повторная доставка схлопнется при
-- слиянии. event_id последним: он уникален и, стоя первым, испортил бы сжатие
-- и разрежённый индекс.
ORDER BY (event_type, toDate(received_at), event_id)
-- Полгода сырых данных: витрины к этому моменту давно всё свернули, а объём
-- сырого потока растёт быстрее всего.
TTL toDateTime(received_at) + INTERVAL 6 MONTH DELETE
SETTINGS index_granularity = 8192;

-- Шардируем по ключу партиционирования Kafka: все события одного пользователя
-- лежат на одном шарде, значит восстановление его пути по сайту и сессионные
-- воронки не требуют межшардовых пересылок. Шардировать сырой поток по film_id
-- нельзя: у кликов, просмотров страниц и поисковых событий фильма нет вовсе.
CREATE TABLE IF NOT EXISTS ugc.raw_events ON CLUSTER '{cluster}'
AS ugc.raw_events_local
ENGINE = Distributed('{cluster}', ugc, raw_events_local, cityHash64(partition_key));
