"""Вспомогательные функции для функциональных тестов."""

import time
import uuid
from typing import Any

import jwt

# Секрет и алгоритм должны совпадать с настройками сервиса выдачи контента
# (rest/core/config.py: authjwt_secret_key='secret', алгоритм по умолчанию HS256).
JWT_SECRET = 'secret'
JWT_ALGORITHM = 'HS256'


def make_film(
    film_id: str | None = None,
    title: str = 'The Star',
    description: str = 'Test description',
    imdb_rating: float = 8.5,
    genres: list[dict] | None = None,
    actors: list[dict] | None = None,
    writers: list[dict] | None = None,
    directors: list[dict] | None = None,
    access_type: str = 'public',
) -> dict[str, Any]:
    """Сформировать документ фильма для индекса movies."""
    genres = genres or [{'id': str(uuid.uuid4()), 'name': 'Action'}]
    actors = actors or [{'id': str(uuid.uuid4()), 'name': 'Ann'}]
    writers = writers or [{'id': str(uuid.uuid4()), 'name': 'Bob'}]
    directors = directors or [{'id': str(uuid.uuid4()), 'name': 'Carl'}]
    return {
        'id': film_id or str(uuid.uuid4()),
        'imdb_rating': imdb_rating,
        'title': title,
        'description': description,
        'access_type': access_type,
        'genres': genres,
        'actors': actors,
        'writers': writers,
        'directors': directors,
        'actors_names': ' '.join(a['name'] for a in actors),
        'writers_names': ' '.join(w['name'] for w in writers),
        'directors_names': ' '.join(d['name'] for d in directors),
    }


def make_access_token(
    roles: list[str] | None = None,
    user_id: str | None = None,
    expires_in: int = 3600,
) -> str:
    """Сформировать валидный access-токен, совместимый с async_fastapi_jwt_auth.

    Токен подписан общим секретом, поэтому сервис выдачи контента проверяет его
    локально — без обращения к Auth-сервису.
    """
    now = int(time.time())
    payload = {
        'sub': user_id or str(uuid.uuid4()),
        'iat': now,
        'nbf': now,
        'exp': now + expires_in,
        'jti': str(uuid.uuid4()),
        'type': 'access',
        'fresh': False,
        'roles': roles or [],
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def auth_header(roles: list[str] | None = None, **kwargs) -> dict[str, str]:
    """Заголовок Authorization с Bearer-токеном для указанных ролей."""
    return {'Authorization': f'Bearer {make_access_token(roles, **kwargs)}'}


def make_person(
    person_id: str | None = None,
    full_name: str = 'John Doe',
) -> dict[str, Any]:
    """Сформировать документ персоны для индекса persons."""
    return {
        'id': person_id or str(uuid.uuid4()),
        'full_name': full_name,
    }


def make_genre(
    genre_id: str | None = None,
    name: str = 'Action',
    description: str = 'Test genre description',
) -> dict[str, Any]:
    """Сформировать документ жанра для индекса genres."""
    return {
        'id': genre_id or str(uuid.uuid4()),
        'name': name,
        'description': description,
    }


def get_es_bulk_query(docs: list[dict], index: str, id_field: str = 'id') -> list[dict]:
    """Сформировать bulk-операции для Elasticsearch."""
    bulk_query: list[dict] = []
    for doc in docs:
        bulk_query.append({'index': {'_index': index, '_id': doc[id_field]}})
        bulk_query.append(doc)
    return bulk_query
