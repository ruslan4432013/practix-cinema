import datetime
import logging
from collections.abc import Iterator
from typing import Any

import psycopg
from psycopg import ClientCursor, OperationalError
from psycopg.rows import dict_row

from etl.gears.movies.extractor import EPOCH_START, PG_DSL, _normalize
from etl.lib.backoff import backoff
from etl.lib.storage import State

logger = logging.getLogger(__name__)

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


class GenresExtractor:
    """Извлекает изменённые жанры из Postgres пачками."""

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
        raw = self.state.get_state(STATE_KEY_GENRES_MODIFIED)
        if not raw:
            return EPOCH_START
        return _normalize(datetime.datetime.fromisoformat(raw))

    @backoff(exceptions=(OperationalError,))
    def _fetch(self, last_modified: datetime.datetime) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(SQL_FETCH_GENRES, (last_modified, self.batch_size))
                return cursor.fetchall()
        except OperationalError:
            try:
                conn.close()
            finally:
                self._conn = None
            raise

    def extract(self) -> Iterator[list[dict[str, Any]]]:
        """Генератор сырых строк жанров из Postgres."""
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
        self.state.set_state(STATE_KEY_GENRES_MODIFIED, last_modified.isoformat())
        logger.info(
            'Состояние обновлено: %s = %s',
            STATE_KEY_GENRES_MODIFIED,
            last_modified.isoformat(),
        )
