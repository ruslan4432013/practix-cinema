"""Разбор конверта события.

Контракт конверта (``EventEnvelope``) описан в
``analytics_collector/src/models/events.py`` и здесь продублирован намеренно.
Общий пакет моделей связал бы два независимо выкатываемых сервиса: обновление
коллектора требовало бы одновременного обновления ETL. Вместо этого —
дублирование плюс две защиты: проверка ``schema_version`` (см. ниже) и
функциональный тест, который гоняет через ETL сообщение, произведённое
настоящим коллектором.

Главный принцип модуля: **разбор никогда не бросает исключение наружу**. Любое
нераспознанное сообщение превращается в ``InvalidEvent`` и уезжает в карантин.
Иначе один битый байт в топике остановил бы конвейер навсегда: ETL падал бы на
одном и том же оффсете, перезапускался и падал снова.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from practix_contracts.v1 import REQUIRED_FIELDS
from practix_contracts.v1.event_types import FILM_VIEW_EVENT_TYPES as _FILM_VIEW_EVENT_TYPES
from practix_contracts.v1.event_types import KNOWN_EVENT_TYPES as _KNOWN_EVENT_TYPES
from practix_contracts.v1.partition_key import partition_key
from practix_etl_clickhouse.core.config import settings

# Типы событий, которые ETL умеет разбирать. Незнакомый тип — не ошибка данных,
# а нормальное следствие того, что коллектор выкатили раньше ETL, поэтому
# событие едет в карантин, а конвейер продолжает работать.
# Словарь типов — из контракта. Раньше эти шесть строк были набраны здесь
# руками второй раз: расхождение с коллектором проявилось бы не падением, а
# ростом ugc.invalid_events с причиной unknown_event_type.
KNOWN_EVENT_TYPES = _KNOWN_EVENT_TYPES

# Типы, из которых собирается таблица просмотров.
#
# video_quality_change сюда НЕ входит, хотя тоже относится к фильму. У этого
# события нет ``duration_ms``, и коллектор не считает для него
# ``completion_rate``: строка витрины получалась бы с progress_pct = 0 —
# то есть «зритель не сдвинулся с начала», чего событие вовсе не утверждает.
# На uniq(view_id) такие строки не влияли, но любое среднее по
# progress_pct/completion_rate тянули к нулю, и заметить это можно было только
# зная, что смену качества надо отфильтровать. Восстанавливать длительность из
# соседних событий сессии потоковый ETL не может (это состояние на сессию), а
# записывать заведомо неверный ноль хуже, чем не записывать ничего: смена
# качества целиком лежит в ugc.raw_events, где у неё есть и позиция, и оба
# качества.
FILM_VIEW_EVENT_TYPES = _FILM_VIEW_EVENT_TYPES

_REQUIRED_FIELDS = REQUIRED_FIELDS


@dataclass(slots=True)
class ParsedEvent:
    """Разобранное событие.

    ``slots=True`` не косметика: объектов здесь десятки тысяч в секунду, и
    отсутствие ``__dict__`` у каждого экономит заметную долю RSS.
    """

    event_id: uuid.UUID
    event_type: str
    schema_version: int
    event_timestamp: datetime | None
    received_at: datetime
    user_id: uuid.UUID | None
    is_authenticated: int
    anonymous_id: str
    session_id: str
    partition_key: str
    context: dict[str, Any]
    payload: dict[str, Any]
    payload_raw: str


@dataclass(slots=True)
class InvalidEvent:
    """Сообщение, которое не удалось разобрать."""

    error_kind: str
    error_text: str


def _parse_dt(value: Any) -> datetime | None:
    """Разбирает ISO-время в tz-aware UTC.

    Наивный datetime драйвер интерпретирует в таймзоне колонки, что даёт сдвиг
    на несколько часов — ошибку, которую замечают через месяц по «странным»
    суточным профилям. Поэтому таймзона проставляется всегда и явно.
    """
    if not value:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _partition_key(data: dict[str, Any]) -> str:
    """Восстанавливает ключ партиционирования по правилу из контракта.

    Значение восстанавливается, а не читается из ключа сообщения Kafka: ключ
    может быть пустым у сообщений, пришедших в обход коллектора, а поле нужно как
    ключ шардирования сырой таблицы.

    Правило живёт в ``practix_contracts.v1.partition_key`` — то же, которым
    коллектор ключ ПРОСТАВЛЯЕТ. Разойдись эти две реализации, события одного
    пользователя поехали бы в разные шарды ClickHouse, и ReplacingMergeTree
    перестал бы схлопывать дубли: запросы продолжали бы работать и молча
    возвращать неверные числа.
    """
    return partition_key(data.get('user_id'), data.get('anonymous_id'), data.get('session_id'))


def parse(value: bytes | None) -> ParsedEvent | InvalidEvent:
    """Разбирает тело сообщения Kafka."""
    if not value:
        return InvalidEvent('json_decode', 'empty message body')

    try:
        data = json.loads(value)
    except (ValueError, TypeError) as exc:
        return InvalidEvent('json_decode', str(exc))

    if not isinstance(data, dict):
        return InvalidEvent('schema', f'expected a JSON object, got {type(data).__name__}')

    missing = [field for field in _REQUIRED_FIELDS if not data.get(field)]
    if missing:
        return InvalidEvent('schema', f'missing required fields: {", ".join(missing)}')

    event_type = str(data['event_type'])
    if event_type not in KNOWN_EVENT_TYPES:
        return InvalidEvent('unknown_event_type', event_type)

    # Версия схемы — единственное, что защищает от тихого неверного разбора.
    # Без этой проверки конверт v2 с переименованными полями разобрался бы по
    # правилам v1 и лёг бы в хранилище молча испорченным.
    try:
        schema_version = int(data.get('schema_version', 1))
    except (TypeError, ValueError):
        return InvalidEvent('schema_version', repr(data.get('schema_version')))
    if schema_version > settings.ETL_MAX_SCHEMA_VERSION:
        return InvalidEvent(
            'schema_version',
            f'schema_version={schema_version} is newer than supported {settings.ETL_MAX_SCHEMA_VERSION}',
        )

    try:
        event_id = uuid.UUID(str(data['event_id']))
        user_id = uuid.UUID(str(data['user_id'])) if data.get('user_id') else None
        received_at = _parse_dt(data['received_at'])
        event_timestamp = _parse_dt(data.get('event_timestamp'))
    except (ValueError, TypeError) as exc:
        return InvalidEvent('schema', str(exc))

    if received_at is None:
        return InvalidEvent('schema', 'received_at is not a valid timestamp')

    payload = data.get('payload') or {}
    if not isinstance(payload, dict):
        return InvalidEvent('schema', 'payload must be an object')
    context = data.get('context') or {}
    if not isinstance(context, dict):
        return InvalidEvent('schema', 'context must be an object')

    return ParsedEvent(
        event_id=event_id,
        event_type=event_type,
        schema_version=schema_version,
        event_timestamp=event_timestamp,
        received_at=received_at,
        user_id=user_id,
        is_authenticated=1 if data.get('is_authenticated') else 0,
        anonymous_id=str(data.get('anonymous_id') or ''),
        session_id=str(data['session_id']),
        partition_key=_partition_key(data),
        context=context,
        payload=payload,
        # Сырой payload сохраняем в хранилище как есть: поле, которое сегодня
        # никем не разобрано, завтра понадобится аналитику, а перечитать Kafka
        # через месяц уже нельзя.
        payload_raw=json.dumps(payload, ensure_ascii=False, default=str),
    )
