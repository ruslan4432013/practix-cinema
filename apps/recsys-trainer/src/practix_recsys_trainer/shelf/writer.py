"""Запись витрины: новая версия → пачки строк → переключение указателя.

ТРИ ТРЕБОВАНИЯ ТЗ ЗАКРЫВАЮТСЯ ОДНОЙ КОНСТРУКЦИЕЙ.

*Идемпотентность (F0.1).* Батч никогда не пишет поверх существующего: он
заводит новую версию и наполняет её. Версия входит в первичный ключ каждой
таблицы списков, поэтому повторный запуск физически не может удвоить строки —
не потому, что мы аккуратно написали ``ON CONFLICT``, а потому, что он пишет в
другое место. Второй тик планировщика на том же слоте отсекается ещё раньше:
``run_key`` уникален.

*Целостность чтения (F0.2).* Указатель переставляется одной транзакцией и
только на версию со статусом ``ready``. Выдача, прочитавшая номер версии,
видит либо весь старый батч, либо весь новый, но никогда не смесь.

*Ровно один писатель (5.3).* ``pg_advisory_lock`` на константе. Обучение
горизонтально не масштабируется и не должно: два писателя означают две версии,
собранные из пересекающихся данных, и идемпотентность перестаёт быть
достижимой в принципе.

ВСТАВКА ИДЁТ ПАЧКАМИ, а не одним оператором. Витрина на 200 000 пользователей —
это четыре миллиона строк; одна транзакция на всё держала бы блокировки и
раздувала WAL единым куском ровно так же, как неограниченный DELETE в
шортенере (см. его README). Пачками — это ещё и осмысленный прогресс в логе.
"""

import datetime
import json
import logging
from collections.abc import Iterable, Iterator
from contextlib import contextmanager

from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import Connection, Engine

from practix_recsys_trainer.core.config import settings

logger = logging.getLogger(__name__)

# Произвольная константа: важно лишь, чтобы её не занял кто-то ещё в этой базе.
# База принадлежит витрине целиком, других претендентов на блокировку нет.
WRITER_LOCK_ID = 776_101_020


class VersionAlreadyBuilt(Exception):
    """Версия с таким ``run_key`` уже существует — прогон на этом слоте состоялся."""


def make_engine() -> Engine:
    return create_engine(settings.shelf_dsn, pool_pre_ping=True, future=True)


@contextmanager
def writer_lock(conn: Connection) -> Iterator[None]:
    """Эксклюзивная блокировка писателя на время всего прогона.

    ``pg_advisory_lock``, а не ``pg_try_advisory_lock``: второй процесс должен
    подождать и увидеть уже готовую версию, а не тихо уйти, оставив расписание
    невыполненным.
    """
    conn.execute(text('SELECT pg_advisory_lock(:key)'), {'key': WRITER_LOCK_ID})
    try:
        yield
    finally:
        conn.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': WRITER_LOCK_ID})


def open_version(conn: Connection, run_key: str, models: str) -> int:
    """Заводит версию в статусе ``building``.

    Конфликт по ``run_key`` — не ошибка прогона, а сообщение «этот слот уже
    отработан»; вызывающий решает, что с этим делать (повторный тик — просто
    выходит).
    """
    row = conn.execute(
        text(
            """
            INSERT INTO shelf_version (run_key, status, models)
            VALUES (:run_key, 'building', :models)
            ON CONFLICT (run_key) DO NOTHING
            RETURNING version
            """
        ),
        {'run_key': run_key, 'models': models},
    ).first()
    if row is None:
        raise VersionAlreadyBuilt(run_key)
    return int(row[0])


def write_similar(conn: Connection, version: int, rows: Iterable[tuple[str, int, str, float]]) -> int:
    return _insert_batched(
        conn,
        'INSERT INTO similar_item (version, film_id, rank, rec_film_id, score) '
        'VALUES (:version, :film_id, :rank, :rec_film_id, :score)',
        (
            {'version': version, 'film_id': film_id, 'rank': rank, 'rec_film_id': rec_film_id, 'score': score}
            for film_id, rank, rec_film_id, score in rows
        ),
    )


def write_personal(conn: Connection, version: int, rows: Iterable[tuple[str, int, str, float]]) -> int:
    return _insert_batched(
        conn,
        'INSERT INTO personal_item (version, user_id, rank, film_id, score) '
        'VALUES (:version, :user_id, :rank, :film_id, :score)',
        (
            {'version': version, 'user_id': user_id, 'rank': rank, 'film_id': film_id, 'score': score}
            for user_id, rank, film_id, score in rows
        ),
    )


def write_popular(conn: Connection, version: int, rows: Iterable[tuple[int, str, float]]) -> int:
    return _insert_batched(
        conn,
        'INSERT INTO popular_item (version, rank, film_id, score) VALUES (:version, :rank, :film_id, :score)',
        ({'version': version, 'rank': rank, 'film_id': film_id, 'score': score} for rank, film_id, score in rows),
    )


def write_catalog(conn: Connection, version: int, film_ids: Iterable[str]) -> int:
    return _insert_batched(
        conn,
        'INSERT INTO catalog_film (version, film_id) VALUES (:version, :film_id)',
        ({'version': version, 'film_id': film_id} for film_id in film_ids),
    )


def publish(conn: Connection, version: int, stats: dict) -> None:
    """Помечает версию готовой и переставляет на неё указатель — одной транзакцией.

    Порядок внутри важен: сначала ``ready``, потом указатель. Обратный порядок
    оставил бы окно, в котором выдача видит указатель на версию, ещё не
    объявленную готовой, — а чтение состояния фильтрует именно по статусу и
    отдало бы «витрины нет».
    """
    conn.execute(
        text(
            """
            UPDATE shelf_version
               SET status = 'ready', finished_at = now(), stats = CAST(:stats AS jsonb)
             WHERE version = :version
            """
        ),
        {'version': version, 'stats': json.dumps(stats, ensure_ascii=False)},
    )
    conn.execute(
        text('UPDATE shelf_pointer SET version = :version, switched_at = now() WHERE id = true'),
        {'version': version},
    )


def mark_failed(conn: Connection, version: int, reason: str) -> None:
    """Провалившаяся версия остаётся в таблице, но указатель на неё не встаёт.

    Удалять её было бы удобнее и хуже: строка со статусом ``failed`` и причиной
    — это то, по чему потом видно, что обучение шло и падало, а не просто не
    запускалось.
    """
    conn.execute(
        text(
            """
            UPDATE shelf_version
               SET status = 'failed', finished_at = now(), stats = CAST(:stats AS jsonb)
             WHERE version = :version
            """
        ),
        {'version': version, 'stats': json.dumps({'error': reason}, ensure_ascii=False)},
    )


def prune_versions(conn: Connection, keep: int) -> int:
    """Уборка старых версий. Актуальную не трогает — и это защищено дважды.

    Явным исключением текущей версии здесь и ``ON DELETE RESTRICT`` у указателя
    в схеме. Второе — не перестраховка: удаление правильной строки по неверному
    условию не должно зависеть от корректности этого запроса.

    Списки удаляются ПЕРЕД версией. Обратный порядок при падении процесса между
    двумя операторами оставил бы строки, на чью версию уже ничто не ссылается,
    — а найти их потом можно только сравнением двух таблиц вручную.
    """
    doomed = [
        int(row[0])
        for row in conn.execute(
            text(
                """
                SELECT version FROM shelf_version
                 WHERE version NOT IN (SELECT version FROM shelf_version ORDER BY version DESC LIMIT :keep)
                   AND version IS DISTINCT FROM (SELECT version FROM shelf_pointer WHERE id = true)
                """
            ),
            {'keep': keep},
        ).all()
    ]
    if not doomed:
        return 0

    for table in ('similar_item', 'personal_item', 'popular_item', 'catalog_film'):
        # Имя таблицы приходит из кортежа-константы выше, а не извне.
        stmt = text(f'DELETE FROM {table} WHERE version IN :versions').bindparams(bindparam('versions', expanding=True))
        conn.execute(stmt, {'versions': doomed})

    conn.execute(
        text('DELETE FROM shelf_version WHERE version IN :versions').bindparams(bindparam('versions', expanding=True)),
        {'versions': doomed},
    )
    return len(doomed)


def read_state(conn: Connection) -> tuple[int | None, datetime.datetime | None, dict]:
    """Актуальная версия, время её раскладки и её статистика.

    Статистика читается ВМЕСТЕ с версией намеренно. Метрики покрытия и размеров
    описывают ВИТРИНУ, а не процесс, который их посчитал: на стенде обучение
    часто запускают руками отдельной командой, и гейдж, заполняемый только
    собственным прогоном демона, показывал бы ноль при живой и полной витрине.
    Алерт RecsCatalogCoverageLow при этом срабатывал бы на пустом месте — а
    алерт, который врёт, перестают читать.
    """
    row = conn.execute(
        text(
            """
            SELECT v.version, v.finished_at, v.stats
              FROM shelf_pointer p
              JOIN shelf_version v ON v.version = p.version
             WHERE p.id = true AND v.status = 'ready'
            """
        )
    ).first()
    if row is None:
        return None, None, {}
    return int(row[0]), row[1], row[2] or {}


def _insert_batched(conn: Connection, statement: str, rows: Iterator[dict]) -> int:
    """Вставка пачками фиксированного размера. Возвращает число записанных строк."""
    stmt = text(statement)
    batch: list[dict] = []
    written = 0
    for row in rows:
        batch.append(row)
        if len(batch) >= settings.RECS_TRAINER_WRITE_BATCH:
            conn.execute(stmt, batch)
            written += len(batch)
            batch = []
    if batch:
        conn.execute(stmt, batch)
        written += len(batch)
    return written
