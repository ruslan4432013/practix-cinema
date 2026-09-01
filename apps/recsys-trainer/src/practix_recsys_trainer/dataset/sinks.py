"""Два слива синтетики, и они отвечают на разные вопросы.

``clickhouse`` — массовый объём прямо в ``ugc.film_views``: десятки тысяч
сеансов за минуты. Это источник, из которого учится модель, и его задача —
дать матрице содержание, а не проверить конвейер.

``api`` — малая порция через БОЕВОЙ путь: коллектор → Kafka → ETL → ClickHouse.
Проверяет ровно то, что прямая вставка проверить не может: что событие
``video_progress`` с нашими полями действительно доезжает до таблицы, из
которой мы потом учимся. Гнать так весь объём бессмысленно — на порядок
медленнее и упирается в rate limit nginx, — а не гнать вовсе значило бы
поверить в стык на слово.

ПОЧЕМУ ПРЯМАЯ ВСТАВКА НЕ ПИШЕТ В ``ugc.raw_events``. Сырые события — епархия
ETL: их состав, кодирование payload и провенанс (топик, партиция, оффсет)
принадлежат ему, и вторая реализация того же преобразования разошлась бы с
первой при первом же изменении контракта. Путь ``api`` наполняет
``raw_events`` по-настоящему, и этого достаточно.
"""

import datetime
import logging
import uuid
from collections.abc import Iterable, Iterator

import httpx
from clickhouse_connect.driver.client import Client

from practix_core.context import REQUEST_ID_HEADER
from practix_recsys_trainer.core.config import settings
from practix_recsys_trainer.dataset.synthetic import ViewSession

logger = logging.getLogger(__name__)

# Порядок обязан совпадать с FILM_VIEW_COLUMNS в
# apps/etl-clickhouse/src/practix_etl_clickhouse/transform/film_views.py.
# ingested_at не перечисляется: у колонки DEFAULT now64(3), и задавать его
# вручную значило бы подменить версию строки в ReplacingMergeTree.
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

# Сколько меток прогресса писать на сеанс. Живой плеер шлёт их десятками, но
# для обучения важен максимум доли просмотра, а не плотность меток: писать
# двадцать строк вместо четырёх значит в пять раз удлинить заливку ради
# данных, которыми модель не пользуется.
PROGRESS_TICKS = 4


def _progress_pct(completion: float) -> int:
    """Бакет прогресса с шагом 5% — та же арифметика, что в ETL."""
    return min(int(completion * 100) // 5 * 5, 100)


def film_view_rows(session: ViewSession) -> Iterator[list]:
    """Сеанс в строки ``ugc.film_views``: несколько меток прогресса и, если досмотрел, событие досмотра."""
    for tick in range(1, PROGRESS_TICKS + 1):
        completion = round(session.completion_rate * tick / PROGRESS_TICKS, 4)
        position = int(session.duration_ms * completion)
        event_time = session.started_at + datetime.timedelta(milliseconds=position)
        yield [
            uuid.uuid4(),
            'video_progress',
            uuid.UUID(session.film_id),
            uuid.UUID(session.user_id),
            session.session_id,
            session.view_id,
            event_time,
            event_time,
            position,
            session.duration_ms,
            position,
            completion,
            _progress_pct(completion),
            session.quality,
            session.device_type,
            1,
        ]

    if session.completed:
        event_time = session.started_at + datetime.timedelta(milliseconds=session.watched_ms)
        yield [
            uuid.uuid4(),
            'video_completed',
            uuid.UUID(session.film_id),
            uuid.UUID(session.user_id),
            session.session_id,
            session.view_id,
            event_time,
            event_time,
            session.watched_ms,
            session.duration_ms,
            session.watched_ms,
            session.completion_rate,
            _progress_pct(session.completion_rate),
            session.quality,
            session.device_type,
            1,
        ]


def to_clickhouse(client: Client, sessions: Iterable[ViewSession], *, batch_size: int = 20_000) -> int:
    """Пакетная вставка в ``ugc.film_views``. Возвращает число записанных строк."""
    written = 0
    batch: list[list] = []
    for session in sessions:
        batch.extend(film_view_rows(session))
        if len(batch) >= batch_size:
            written += _flush(client, batch)
            batch = []
    if batch:
        written += _flush(client, batch)
    return written


def _flush(client: Client, rows: list[list]) -> int:
    client.insert(
        'film_views',
        rows,
        column_names=list(FILM_VIEW_COLUMNS),
        settings={
            # Distributed-вставка по умолчанию асинхронна: она подтвердит
            # запись раньше, чем шард её примет, и следующий же SELECT увидит
            # пустую таблицу. Ровно та же настройка стоит в боевом ETL.
            'distributed_foreground_insert': 1,
        },
    )
    return len(rows)


def to_api(sessions: Iterable[ViewSession], *, batch_size: int = 20) -> tuple[int, int]:
    """Отправка через боевой путь коллектора. Возвращает (принято, отклонено).

    Событие уходит БЕЗ ``user_id``: коллектор берёт его только из проверенного
    токена и режет поле в запросе (``extra='forbid'``). Синтетика поэтому
    приезжает в ClickHouse анонимной, и для обучения она не годится — но
    проверяется здесь не обучение, а живучесть стыка.
    """
    accepted = 0
    rejected = 0
    with httpx.Client(base_url=settings.RECS_TRAINER_COLLECTOR_URL.rstrip('/'), timeout=10.0) as client:
        batch: list[dict] = []
        for session in sessions:
            batch.append(_api_event(session))
            if len(batch) >= batch_size:
                ok, bad = _post(client, batch)
                accepted += ok
                rejected += bad
                batch = []
        if batch:
            ok, bad = _post(client, batch)
            accepted += ok
            rejected += bad
    return accepted, rejected


def _api_event(session: ViewSession) -> dict:
    return {
        'event_type': 'video_progress',
        'session_id': session.session_id,
        'film_id': session.film_id,
        'playback_position_ms': session.watched_ms,
        'duration_ms': session.duration_ms,
        'quality': session.quality,
    }


def _post(client: httpx.Client, batch: list[dict]) -> tuple[int, int]:
    headers = {REQUEST_ID_HEADER: str(uuid.uuid4())}
    try:
        response = client.post('/api/v1/events/batch', json={'events': batch}, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning('COLLECTOR UNAVAILABLE: пачка из %s событий не отправлена (%s)', len(batch), exc)
        return 0, len(batch)
    if response.status_code >= httpx.codes.BAD_REQUEST:
        logger.warning('COLLECTOR REJECTED: %s %s', response.status_code, response.text[:200])
        return 0, len(batch)
    return len(batch), 0
