"""Строка типизированной таблицы ugc.film_views.

Собирается из двух типов событий плеера — метки прогресса и досмотра. Здесь же
считаются два производных поля, ради которых таблица и существует отдельно от
сырой: ``view_id`` (идентификатор сеанса просмотра) и ``progress_pct`` (бакет
прогресса). Оба считаются один раз на этапе загрузки, а не в каждом запросе
аналитика.

Почему смена качества в витрину не попадает — см. ``FILM_VIEW_EVENT_TYPES``.
"""

import uuid
from typing import Any

from transform.envelope import FILM_VIEW_EVENT_TYPES, ParsedEvent

FILM_VIEWS_TABLE = 'film_views'

FILM_VIEW_COLUMNS = (
    'event_id',
    'event_type',
    'film_id',
    'user_id',
    'session_id',
    'view_id',
    'event_time',
    'received_at',
    'playback_position_ms',
    'duration_ms',
    'watched_ms',
    'completion_rate',
    'progress_pct',
    'quality',
    'device_type',
    'is_authenticated',
)

FILM_VIEW_COLUMN_TYPES = (
    'UUID',
    'LowCardinality(String)',
    'UUID',
    'Nullable(UUID)',
    'String',
    'String',
    "DateTime64(3, 'UTC')",
    "DateTime64(3, 'UTC')",
    'UInt32',
    'UInt32',
    'UInt32',
    'Float32',
    'UInt8',
    'LowCardinality(String)',
    'LowCardinality(String)',
    'UInt8',
)

# Шаг бакета кривой досмотра. 5 % — компромисс: 1 % даёт шум и в двадцать раз
# больше строк в витрине, 10 % слишком грубо, чтобы разглядеть точку выхода.
_PROGRESS_BUCKET = 5
_MAX_UINT32 = 4_294_967_295


def _uint32(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or value < 0:
        return 0
    return min(value, _MAX_UINT32)


def to_film_view_row(event: ParsedEvent) -> tuple | None:
    """Возвращает строку просмотра или None, если событие для витрины не годится."""
    if event.event_type not in FILM_VIEW_EVENT_TYPES:
        return None

    raw_film_id = event.payload.get('film_id')
    if not raw_film_id:
        return None
    try:
        film_id = uuid.UUID(str(raw_film_id))
    except (ValueError, TypeError):
        # Событие плеера без валидного film_id для витрин бесполезно, но в
        # сырой таблице оно уже лежит — терять его целиком незачем.
        return None

    payload = event.payload
    duration_ms = _uint32(payload, 'duration_ms')
    watched_ms = _uint32(payload, 'watched_ms')
    position_ms = _uint32(payload, 'playback_position_ms')

    # Для метки прогресса «просмотрено» — это и есть текущая позиция.
    if event.event_type == 'video_progress' and not watched_ms:
        watched_ms = position_ms
    # У досмотра позиции нет, но она равна тому, сколько просмотрено.
    if event.event_type == 'video_completed' and not position_ms:
        position_ms = watched_ms

    # completion_rate считает коллектор (event_service._build_payload) — тому,
    # что прислал клиент, доверять нельзя. Пересчёт здесь — страховка на случай
    # события из старой версии коллектора, где поля ещё не было: оба типа
    # событий витрины несут duration_ms, поэтому доля восстановима.
    completion_rate = payload.get('completion_rate')
    if not isinstance(completion_rate, (int, float)):
        completion_rate = (position_ms / duration_ms) if duration_ms else 0.0
    completion_rate = min(max(float(completion_rate), 0.0), 1.0)

    progress_pct = min(int(completion_rate * 100) // _PROGRESS_BUCKET * _PROGRESS_BUCKET, 100)

    return (
        event.event_id,
        event.event_type,
        film_id,
        event.user_id,
        event.session_id,
        # Сеанс просмотра = пара «сессия + фильм». Считать просмотры по
        # событиям нельзя: метки прогресса идут десятками на один просмотр, и
        # count() измерял бы длину фильма, а не его популярность. Побочный, но
        # важный эффект: uniq(view_id) невосприимчив к повторной доставке.
        f'{event.session_id}:{film_id}',
        event.event_timestamp or event.received_at,
        event.received_at,
        position_ms,
        duration_ms,
        watched_ms,
        completion_rate,
        progress_pct,
        str(payload.get('quality') or ''),
        str(event.context.get('device_type') or ''),
        event.is_authenticated,
    )
