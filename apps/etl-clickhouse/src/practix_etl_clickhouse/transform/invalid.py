"""Строка карантина ugc.invalid_events."""

from practix_etl_clickhouse.transform.envelope import InvalidEvent

INVALID_TABLE = 'invalid_events'

INVALID_COLUMNS = (
    'kafka_topic',
    'kafka_partition',
    'kafka_offset',
    'error_kind',
    'error_text',
    'raw_key',
    'raw_value',
)

INVALID_COLUMN_TYPES = (
    'LowCardinality(String)',
    'UInt16',
    'UInt64',
    'LowCardinality(String)',
    'String',
    'String',
    'String',
)

# Сырое тело нужно целиком, чтобы разобрать инцидент, но битое сообщение может
# оказаться и мегабайтным мусором. Обрезаем до размера, при котором причина
# всё ещё видна.
_MAX_RAW_VALUE = 64 * 1024


def _decode(value: bytes | None) -> str:
    if not value:
        return ''
    return value[:_MAX_RAW_VALUE].decode('utf-8', errors='replace')


def to_invalid_row(
    invalid: InvalidEvent, topic: str, partition: int, offset: int, key: bytes | None, value: bytes | None
) -> tuple:
    return (
        topic,
        partition,
        offset,
        invalid.error_kind,
        invalid.error_text[:1024],
        _decode(key),
        _decode(value),
    )
