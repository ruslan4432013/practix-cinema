"""Базовый инкрементальный экстрактор из Postgres.

Три экстрактора (`movies`, `genres`, `persons`) были структурно ОДИНАКОВЫ:
``__init__``, ``_connect``, ``_get_last_modified``, ``_fetch``, ``extract`` и
``commit`` совпадали побайтово, а различались ровно двумя константами — ключом
состояния и SQL-запросом. jscpd и pylint показывали здесь по три клона (14–16
строк каждый) — последняя группа дублирования, оставшаяся в репозитории.

Заодно исправлено то, что дублирование прятало: `genres` и `persons`
импортировали ``EPOCH_START``, ``PG_DSL`` и **приватную** ``_normalize`` из
``gears/movies/extractor.py``. То есть два конвейера зависели от внутренностей
третьего — если бы кто-то отрефакторил movies, они бы сломались, и связь эта
нигде не была обозначена.

Библиотекой в ``libs/`` это НЕ становится: потребитель один — сам сервис
ETL-Elasticsearch. Библиотека на одного потребителя усложняет, а не упрощает.
"""

import datetime
import logging
from collections.abc import Iterator
from typing import Any, ClassVar

import psycopg
from psycopg import ClientCursor, OperationalError
from psycopg.rows import dict_row

from practix_etl_elasticsearch.lib.backoff import backoff
from practix_etl_elasticsearch.lib.storage import State
from practix_etl_elasticsearch.settings import settings

logger = logging.getLogger(__name__)

EPOCH_START = datetime.datetime(1970, 1, 1)

PG_DSL = {
    'dbname': settings.POSTGRES_DB,
    'user': settings.POSTGRES_USER,
    'password': settings.POSTGRES_PASSWORD,
    'host': settings.POSTGRES_HOST,
    'port': settings.POSTGRES_PORT,
}


def normalize_datetime(dt: datetime.datetime) -> datetime.datetime:
    """Приводит datetime к naive (без tz), чтобы сравнивать с ``modified`` из БД.

    Публичная (без подчёркивания) — раньше два экстрактора импортировали её как
    приватную из чужого модуля.
    """
    if dt.tzinfo is not None:
        return dt.astimezone(datetime.UTC).replace(tzinfo=None)
    return dt


class IncrementalExtractor:
    """Читает изменённые с прошлого запуска строки пачками, по метке ``modified``.

    Наследник задаёт две вещи:

    * ``state_key`` — под каким ключом хранится метка последней обработанной
      строки. Ключи РАЗНЫЕ у сущностей намеренно: конвейеры продвигаются
      независимо, и общий ключ означал бы, что отставание одного двигает
      остальные.
    * ``sql`` — запрос с двумя параметрами: метка и размер пачки.
    """

    state_key: ClassVar[str]
    sql: ClassVar[str]
    #: Только для сообщений в логах и докстрингов.
    entity_name: ClassVar[str] = 'строк'

    def __init__(self, state: State, batch_size: int) -> None:
        self.state = state
        self.batch_size = batch_size
        self._conn: psycopg.Connection | None = None

    def _connect(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            logger.info('Подключаемся к Postgres %s:%s', PG_DSL['host'], PG_DSL['port'])
            self._conn = psycopg.connect(**PG_DSL, row_factory=dict_row, cursor_factory=ClientCursor)
        return self._conn

    def _get_last_modified(self) -> datetime.datetime:
        raw = self.state.get_state(self.state_key)
        if not raw:
            return EPOCH_START
        return normalize_datetime(datetime.datetime.fromisoformat(raw))

    @backoff(exceptions=(OperationalError,))
    def _fetch(self, last_modified: datetime.datetime) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            with conn.cursor() as cursor:
                cursor.execute(self.sql, (last_modified, self.batch_size))
                return cursor.fetchall()
        except OperationalError:
            # Соединение сбрасывается, чтобы следующая попытка (её сделает
            # backoff) переподключилась, а не переиспользовала мёртвое.
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

            # Состояние продвигает загрузчик (через commit) — только ПОСЛЕ
            # успешной записи в Elasticsearch. Если метка не сдвинулась, значит
            # пачка не доехала, и повторять тот же запрос бессмысленно.
            new_last = self._get_last_modified()
            if new_last <= last_modified:
                return

    def commit(self, rows: list[dict[str, Any]]) -> None:
        """Сохранить состояние после успешной загрузки пачки."""
        last_modified = max(row['modified'] for row in rows)
        self.state.set_state(self.state_key, last_modified.isoformat())
        logger.info('Состояние обновлено: %s = %s', self.state_key, last_modified.isoformat())
