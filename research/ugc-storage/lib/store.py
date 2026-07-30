"""
Общий контракт хранилища и список сценариев.

Три адаптера различаются только телами запросов; всё остальное — список
сценариев, их формулировки, цикл замера, статистика — живёт здесь и в
benchmark.py в одном экземпляре. Это не только чище: в репозитории включён
контроль дублирования (`.jscpd.json`), и три почти одинаковых класса его бы
пробили.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Protocol, runtime_checkable

# Порядок сортировки рецензий. Алгоритм ранжирования ещё будет меняться —
# поэтому это список, а не одна «правильная» сортировка.
REVIEW_ORDERS = {
    'new': 'created_at',
    'useful': 'useful_score',
    'rating': 'author_rating',
}

# (код, описание). Описания попадают в таблицы README, поэтому формулируются
# один раз и здесь.
READ_SCENARIOS: list[tuple[str, str]] = [
    ('R1', 'Список понравившихся пользователю фильмов (оценка ≥ 8, топ-50)'),
    ('R2', 'Количество лайков и дизлайков у фильма (агрегация на лету)'),
    ('R3', 'Средняя пользовательская оценка фильма (агрегация на лету)'),
    ('R3c', 'Средняя пользовательская оценка фильма из преагрегата'),
    ('R4', 'Список закладок пользователя'),
    ('R5-new', 'Рецензии фильма, сортировка по дате (топ-20)'),
    ('R5-useful', 'Рецензии фильма, сортировка по полезности (топ-20)'),
    ('R5-rating', 'Рецензии фильма, сортировка по оценке автора (топ-20)'),
]

WRITE_SCENARIOS: list[tuple[str, str]] = [
    ('W1', 'Поставить новую оценку (лайк + преагрегат)'),
    ('W1u', 'Изменить уже поставленную оценку'),
    ('W2', 'End-to-end: оценка записана и сразу видна в преагрегате'),
]


@runtime_checkable
class Store(Protocol):
    """Минимум, который обязано уметь хранилище-кандидат."""

    name: str

    # --- запись ------------------------------------------------------------
    def write_likes(self, rows: list[dict]) -> None: ...
    def write_bookmarks(self, rows: list[dict]) -> None: ...
    def write_reviews(self, rows: list[dict]) -> None: ...
    def write_review_votes(self, rows: list[dict]) -> None: ...
    def write_film_rating(self, rows: list[dict]) -> None: ...

    # --- обслуживание ------------------------------------------------------
    def truncate(self) -> None: ...
    def counts(self) -> dict[str, int]: ...
    def optimize(self) -> None: ...
    def close(self) -> None: ...

    # --- сценарии чтения ---------------------------------------------------
    def r1_user_liked_films(self, user_id: uuid.UUID) -> list: ...
    def r2_film_like_counts(self, film_id: uuid.UUID) -> tuple[int, int]: ...
    def r3_film_avg_rating(self, film_id: uuid.UUID) -> tuple[float, int]: ...
    def r3c_film_avg_cached(self, film_id: uuid.UUID) -> tuple[float, int]: ...
    def r4_user_bookmarks(self, user_id: uuid.UUID) -> list: ...
    def r5_film_reviews(self, film_id: uuid.UUID, order: str) -> list: ...

    # --- сценарий записи ---------------------------------------------------
    def w1_set_rating(self, user_id: uuid.UUID, film_id: uuid.UUID, rating: int) -> None: ...


def rating_deltas(old_rating: int | None, new_rating: int) -> tuple[int, int, dict[int, int]]:
    """Дельты преагрегата при постановке или изменении оценки.

    Возвращает (Δколичество, Δсумма, {позиция гистограммы: Δ}).

    Вынесено из адаптеров, потому что арифметика одна на всех, а ошибиться в ней
    легко: при изменении оценки количество не меняется, а если новая оценка
    совпала со старой — не меняется вообще ничего, и слепой `+1/-1` по двум
    одинаковым позициям гистограммы дал бы мусор.
    """
    if old_rating is None:
        return 1, new_rating, {new_rating: 1}
    if old_rating == new_rating:
        return 0, 0, {}
    return 0, new_rating - old_rating, {new_rating: 1, old_rating: -1}


def build_store(name: str, **kwargs) -> Store:
    """Импорт драйвера ленивый: замер MongoDB не должен требовать psycopg2."""
    if name == 'mongo':
        from lib.mongo_store import MongoStore

        return MongoStore(**kwargs)
    if name == 'postgres':
        from lib.pg_store import PgStore

        return PgStore(**kwargs)
    if name == 'clickhouse':
        from lib.ch_store import ClickHouseStore

        return ClickHouseStore(**kwargs)
    raise SystemExit(f'Неизвестное хранилище: {name}. Доступны: mongo, postgres, clickhouse')


def chunked(rows: Iterable[dict], size: int) -> Iterable[list[dict]]:
    batch: list[dict] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
