"""Сборка конверта события для тестов ETL.

Конверт строится ВРУЧНУЮ, а не импортом моделей коллектора. Это принципиально:
контракт между коллектором и ETL — сетевой, и тест обязан ловить его
расхождение. Импорт моделей коллектора спрятал бы ровно ту ошибку, ради
которой тест написан: переименовали поле в конверте — оба сервиса «согласны»,
а данные в хранилище испорчены.
"""

import datetime
import json
import uuid

SCHEMA_VERSION = 1

FILM_ID = '3d825f60-9fff-4dfe-b294-1a45fa1e115d'


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def envelope(event_type: str, payload: dict, **overrides) -> dict:
    """Конверт EventEnvelope в том виде, в каком его пишет коллектор.

    Коллектор сериализует с exclude_none=False, то есть все поля присутствуют,
    в том числе null. ETL обязан это переваривать.
    """
    body = {
        'event_id': str(uuid.uuid4()),
        'event_type': event_type,
        'schema_version': SCHEMA_VERSION,
        'event_timestamp': None,
        'received_at': _now(),
        'user_id': None,
        'is_authenticated': False,
        'anonymous_id': f'anon-{uuid.uuid4()}',
        'session_id': f'sess-{uuid.uuid4()}',
        'context': {
            'url': 'http://localhost/player',
            'referrer': None,
            'screen_width': 1920,
            'screen_height': 1080,
            'viewport_width': 1900,
            'viewport_height': 900,
            'locale': 'ru-RU',
            'timezone': 'Europe/Moscow',
            'user_agent': 'Mozilla/5.0',
            'device_type': 'desktop',
            'os': 'Mac OS X',
            'browser': 'Chrome',
            'ip_hash': 'a' * 32,
        },
        'payload': payload,
    }
    body.update(overrides)
    return body


def click(**overrides) -> dict:
    return envelope('click', {'element_type': 'film', 'target_id': FILM_ID, 'position': 3}, **overrides)


def page_view(**overrides) -> dict:
    return envelope(
        'page_view', {'page_type': 'film', 'page_url': 'http://localhost/', 'duration_ms': 4500}, **overrides
    )


def video_progress(film_id: str = FILM_ID, position_ms: int = 30_000, duration_ms: int = 120_000, **overrides) -> dict:
    # completion_rate считает коллектор — ETL берёт его из payload готовым.
    payload = {
        'film_id': film_id,
        'playback_position_ms': position_ms,
        'duration_ms': duration_ms,
        'quality': '1080p',
        'is_paused': False,
        'completion_rate': round(min(position_ms / duration_ms, 1.0), 4),
    }
    return envelope('video_progress', payload, **overrides)


def video_completed(film_id: str = FILM_ID, duration_ms: int = 120_000, watched_ms: int = 118_000, **overrides) -> dict:
    payload = {
        'film_id': film_id,
        'duration_ms': duration_ms,
        'watched_ms': watched_ms,
        'quality': '1080p',
        'completion_rate': round(min(watched_ms / duration_ms, 1.0), 4),
    }
    return envelope('video_completed', payload, **overrides)


def quality_change(film_id: str = FILM_ID, **overrides) -> dict:
    payload = {
        'film_id': film_id,
        'from_quality': '720p',
        'to_quality': '1080p',
        'playback_position_ms': 12_000,
        'is_auto': False,
    }
    return envelope('video_quality_change', payload, **overrides)


def search_filter(**overrides) -> dict:
    return envelope(
        'search_filter_used',
        {'query': 'матрица', 'filters': [{'field': 'genre', 'value': 'sci-fi'}], 'results_count': 7},
        **overrides,
    )


def partition_key(body: dict) -> str:
    """Повторяет EventEnvelope.partition_key коллектора."""
    return str(body.get('user_id') or body.get('anonymous_id') or body['session_id'])


def encode(body: dict) -> bytes:
    return json.dumps(body, ensure_ascii=False).encode('utf-8')
