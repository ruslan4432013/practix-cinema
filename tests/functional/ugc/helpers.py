"""Вспомогательные функции тестов сервиса сбора событий."""

import datetime
import uuid

import jwt

from core.config import settings

FILM_ID = '3d825f60-9fff-4dfe-b294-1a45fa1e115d'


def make_access_token(user_id: str | None = None, roles: list[str] | None = None, expired: bool = False) -> str:
    """Выпускает access-токен, совместимый с async-fastapi-jwt-auth.

    Токен подписывается тем же секретом, что задан сервису: коллектор проверяет
    подпись локально, поэтому поднимать Auth ради тестов не нужно.
    """
    now = datetime.datetime.now(datetime.UTC)
    exp = now - datetime.timedelta(hours=1) if expired else now + datetime.timedelta(hours=1)
    payload = {
        'sub': user_id or str(uuid.uuid4()),
        'iat': now,
        'nbf': now,
        'exp': exp,
        'jti': str(uuid.uuid4()),
        'type': 'access',
        'fresh': False,
        'roles': roles or ['subscriber'],
    }
    return jwt.encode(payload, settings.AUTHJWT_SECRET_KEY, algorithm='HS256')


def auth_header(user_id: str | None = None, **kwargs) -> dict[str, str]:
    return {'Authorization': f'Bearer {make_access_token(user_id, **kwargs)}'}


def click_payload(**overrides) -> dict:
    payload = {
        'session_id': f'sess-{uuid.uuid4()}',
        'anonymous_id': f'anon-{uuid.uuid4()}',
        'element_type': 'film',
        'target_id': FILM_ID,
        'page_url': f'http://localhost/films/{FILM_ID}',
        'position': 3,
    }
    payload.update(overrides)
    return payload


def page_view_payload(**overrides) -> dict:
    payload = {
        'session_id': f'sess-{uuid.uuid4()}',
        'page_type': 'film',
        'page_url': f'http://localhost/films/{FILM_ID}',
        'duration_ms': 45_000,
    }
    payload.update(overrides)
    return payload


def quality_change_payload(**overrides) -> dict:
    payload = {
        'event_type': 'video_quality_change',
        'session_id': f'sess-{uuid.uuid4()}',
        'film_id': FILM_ID,
        'from_quality': '720p',
        'to_quality': '1080p',
        'playback_position_ms': 123_000,
        'is_auto': False,
    }
    payload.update(overrides)
    return payload


def video_progress_payload(**overrides) -> dict:
    payload = {
        'event_type': 'video_progress',
        'session_id': f'sess-{uuid.uuid4()}',
        'film_id': FILM_ID,
        'playback_position_ms': 1_800_000,
        'duration_ms': 7_200_000,
        'quality': '1080p',
    }
    payload.update(overrides)
    return payload


def video_completed_payload(**overrides) -> dict:
    payload = {
        'event_type': 'video_completed',
        'session_id': f'sess-{uuid.uuid4()}',
        'film_id': FILM_ID,
        'duration_ms': 7_200_000,
        'watched_ms': 7_150_000,
    }
    payload.update(overrides)
    return payload


def search_filter_payload(**overrides) -> dict:
    payload = {
        'event_type': 'search_filter_used',
        'session_id': f'sess-{uuid.uuid4()}',
        'query': 'матрица',
        'filters': [{'field': 'genre', 'value': 'sci-fi'}, {'field': 'year', 'value': '1999'}],
        'results_count': 7,
    }
    payload.update(overrides)
    return payload
