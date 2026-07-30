"""Строка сырой таблицы ugc.raw_events.

Имена и типы колонок передаются в драйвер явно и живут рядом с функцией
сборки строки. Полагаться на вывод типов по первой строке нельзя: у
``Nullable(UUID)`` первая строка сплошь и рядом ``None``, и драйвер вывел бы
``Nullable(Nothing)``.
"""

from typing import Any

from transform.envelope import ParsedEvent

RAW_TABLE = 'raw_events'

# Порядок строго совпадает с ddl/10_raw_events.sql. ingested_at не передаётся —
# у него DEFAULT now64() на стороне сервера.
RAW_COLUMNS = (
    'event_id',
    'event_type',
    'schema_version',
    'event_timestamp',
    'received_at',
    'user_id',
    'is_authenticated',
    'anonymous_id',
    'session_id',
    'partition_key',
    'url',
    'referrer',
    'screen_width',
    'screen_height',
    'viewport_width',
    'viewport_height',
    'locale',
    'timezone',
    'user_agent',
    'device_type',
    'os',
    'browser',
    'ip_hash',
    'payload',
    'kafka_topic',
    'origin_topic',
    'kafka_partition',
    'kafka_offset',
)

RAW_COLUMN_TYPES = (
    'UUID',
    'LowCardinality(String)',
    'UInt16',
    "Nullable(DateTime64(3, 'UTC'))",
    "DateTime64(3, 'UTC')",
    'Nullable(UUID)',
    'UInt8',
    'String',
    'String',
    'String',
    'String',
    'String',
    'UInt16',
    'UInt16',
    'UInt16',
    'UInt16',
    'LowCardinality(String)',
    'LowCardinality(String)',
    'String',
    'LowCardinality(String)',
    'LowCardinality(String)',
    'LowCardinality(String)',
    'String',
    'String',
    'LowCardinality(String)',
    'LowCardinality(String)',
    'UInt16',
    'UInt64',
)

# Верхняя граница UInt16 в схеме. Клиент может прислать любую ерунду в
# размерах экрана, а переполнение уронило бы вставку всей пачки из-за одного
# события.
_MAX_UINT16 = 65_535


def _dimension(context: dict[str, Any], key: str) -> int:
    value = context.get(key)
    if not isinstance(value, int) or value < 0:
        return 0
    return min(value, _MAX_UINT16)


def _text(source: dict[str, Any], key: str) -> str:
    """Пустая строка вместо NULL.

    В сырой таблице почти все текстовые поля необязательны, но Nullable(String)
    в ClickHouse стоит дополнительной колонки-маски и мешает LowCardinality.
    Для аналитики разница между «не прислали» и «прислали пустое» здесь
    несущественна.
    """
    value = source.get(key)
    return '' if value is None else str(value)


def to_raw_row(event: ParsedEvent, topic: str, origin_topic: str, partition: int, offset: int) -> tuple:
    context = event.context
    return (
        event.event_id,
        event.event_type,
        event.schema_version,
        event.event_timestamp,
        event.received_at,
        event.user_id,
        event.is_authenticated,
        event.anonymous_id,
        event.session_id,
        event.partition_key,
        _text(context, 'url'),
        _text(context, 'referrer'),
        _dimension(context, 'screen_width'),
        _dimension(context, 'screen_height'),
        _dimension(context, 'viewport_width'),
        _dimension(context, 'viewport_height'),
        _text(context, 'locale'),
        _text(context, 'timezone'),
        _text(context, 'user_agent'),
        _text(context, 'device_type'),
        _text(context, 'os'),
        _text(context, 'browser'),
        _text(context, 'ip_hash'),
        event.payload_raw,
        topic,
        origin_topic,
        partition,
        offset,
    )
