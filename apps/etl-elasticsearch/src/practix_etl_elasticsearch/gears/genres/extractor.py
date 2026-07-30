"""Извлечение изменённых жанров из Postgres.

Общий механизм — в ``gears/base/extractor.py``. Раньше этот модуль импортировал
``EPOCH_START``, ``PG_DSL`` и приватную ``_normalize`` из экстрактора ФИЛЬМОВ, то
есть зависел от внутренностей соседнего конвейера.
"""

from practix_etl_elasticsearch.gears.base.extractor import IncrementalExtractor

STATE_KEY_GENRES_MODIFIED = 'genre_last_modified'

SQL_FETCH_GENRES = """
SELECT
    g.id,
    g.name,
    g.description,
    g.modified
FROM content.genre g
WHERE g.modified > %s
ORDER BY g.modified
LIMIT %s;
"""


class GenresExtractor(IncrementalExtractor):
    """Извлекает изменённые жанры из Postgres пачками."""

    state_key = STATE_KEY_GENRES_MODIFIED
    sql = SQL_FETCH_GENRES
    entity_name = 'жанров'
