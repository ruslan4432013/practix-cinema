-- Карантин для отравленных сообщений.
--
-- Одно битое сообщение в топике не имеет права останавливать конвейер: это
-- классический способ похоронить ETL навсегда — он падает на одном и том же
-- оффсете, перезапускается и падает снова. Поэтому нераспознанное сообщение
-- едет сюда вместе с сырыми байтами, а оффсет коммитится вместе с пачкой.
--
-- Писать такие сообщения обратно в Kafka-DLQ нельзя: ETL сам читает DLQ, и
-- получилась бы петля.
CREATE TABLE IF NOT EXISTS ugc.invalid_events_local ON CLUSTER '{cluster}'
(
    ingested_at     DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC'),
    kafka_topic     LowCardinality(String),
    kafka_partition UInt16,
    kafka_offset    UInt64,
    -- json_decode | schema | unknown_event_type | schema_version
    error_kind      LowCardinality(String),
    error_text      String,
    raw_key         String,
    -- Сырое тело обязательно: без него разобрать инцидент невозможно, а
    -- перечитать сообщение из Kafka через неделю уже нельзя.
    raw_value       String CODEC(ZSTD(3))
)
ENGINE = ReplicatedMergeTree
PARTITION BY toYYYYMM(ingested_at)
ORDER BY (kafka_topic, kafka_partition, kafka_offset)
TTL toDateTime(ingested_at) + INTERVAL 30 DAY DELETE;

CREATE TABLE IF NOT EXISTS ugc.invalid_events ON CLUSTER '{cluster}'
AS ugc.invalid_events_local
ENGINE = Distributed('{cluster}', ugc, invalid_events_local, cityHash64(kafka_offset));
