import datetime
import logging
from collections.abc import Iterator
from typing import Any

import psycopg
from psycopg import ClientCursor, OperationalError
from psycopg.rows import dict_row

from etl.lib.backoff import backoff
from etl.lib.storage import State
from etl.settings import settings

logger = logging.getLogger(__name__)

EPOCH_START = datetime.datetime(1970, 1, 1)


def _normalize(dt: datetime.datetime) -> datetime.datetime:
    """Приводит datetime к naive (без tz), чтобы сравнивать с modified из БД."""
    if dt.tzinfo is not None:
        return dt.astimezone(datetime.UTC).replace(tzinfo=None)
    return dt


PG_DSL = {
    'dbname': settings.POSTGRES_DB,
    'user': settings.POSTGRES_USER,
    'password': settings.POSTGRES_PASSWORD,
    'host': settings.POSTGRES_HOST,
    'port': settings.POSTGRES_PORT,
}
STATE_KEY_LAST_MODIFIED = 'film_work_last_modified'

SQL_FETCH_FILMS = """
SELECT
    fw.id,
    fw.title,
    fw.description,
    fw.rating,
    fw.type,
    fw.creation_date,
    fw.created,
    fw.modified,
    COALESCE (
        json_agg(
            DISTINCT jsonb_build_object(
                'person_role', pfw.role,
                'person_id', p.id,
                'person_name', p.full_name
            )
        ) FILTER (WHERE p.id IS NOT NULL),
        '[]'
    ) AS persons,
    COALESCE(
        json_agg(json_build_object('id', g.id::text, 'name', g.name)) FILTER (WHERE g.id IS NOT NULL),
        '[]'::json
    ) AS genres
FROM content.film_work fw
LEFT JOIN content.person_film_work pfw ON pfw.film_work_id = fw.id
LEFT JOIN content.person p ON p.id = pfw.person_id
LEFT JOIN content.genre_film_work gfw ON gfw.film_work_id = fw.id
LEFT JOIN content.genre g ON g.id = gfw.genre_id
WHERE fw.modified > %s
GROUP BY fw.id
ORDER BY fw.modified
LIMIT %s;
"""


class PostgresExtractor:
    """Извлекает изменённые кинопроизведения из Postgres пачками.

    Хранит позицию (modified последней обработанной записи) в State,
    что позволяет переживать перезапуски и продолжать с того же места.
    """

    def __init__(self, state: State, batch_size: int) -> None:
        self.state = state
        self.batch_size = batch_size
        self._conn: psycopg.Connection | None = None

    @backoff(exceptions=(OperationalError,))
    def _connect(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            logger.info('Подключаемся к Postgres %s:%s', PG_DSL['host'], PG_DSL['port'])
            self._conn = psycopg.connect(**PG_DSL, row_factory=dict_row, cursor_factory=ClientCursor)
        return self._conn

    def _get_last_modified(self) -> datetime.datetime:
        raw = self.state.get_state(STATE_KEY_LAST_MODIFIED)
        if not raw:
            return EPOCH_START
        return _normalize(datetime.datetime.fromisoformat(raw))

    @backoff(exceptions=(OperationalError,))
    def _fetch(self, last_modified: datetime.datetime) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(SQL_FETCH_FILMS, (last_modified, self.batch_size))
                return cursor.fetchall()
        except OperationalError:
            try:
                conn.close()
            finally:
                self._conn = None
            raise

    def extract(self) -> Iterator[list[dict[str, Any]]]:
        """Генератор сырых строк из Postgres."""
        while True:
            last_modified = self._get_last_modified()
            rows = self._fetch(last_modified)
            if not rows:
                return
            yield rows

            new_last = self._get_last_modified()
            if new_last <= last_modified:
                return

    def commit(self, rows: list[dict[str, Any]]) -> None:
        """Сохранить состояние после успешной загрузки пачки."""
        last_modified = max(row['modified'] for row in rows)
        self.state.set_state(STATE_KEY_LAST_MODIFIED, last_modified.isoformat())
        logger.info('Состояние обновлено: %s = %s', STATE_KEY_LAST_MODIFIED, last_modified.isoformat())
