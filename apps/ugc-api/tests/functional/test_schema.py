"""Сверка ORM-моделей с той схемой, которую реально накатила миграция.

Схему создаёт ручной DDL (``migrations/versions/0001_initial_ugc_schema.py``), а
``models/entity.py`` для её создания не используется вовсе. Это два источника
правды, и разойтись они могут молча: модель никто не разворачивает, поэтому
опечатка в ней не падает ни на одном тесте — она всплывает позже, когда по
модели пишут запрос к колонке, которой в базе нет.

Чего этот набор НЕ сравнивает и почему:

* **Типы колонок.** ``ARRAY(Integer)`` против ``integer[]``, ``SmallInteger``
  против ``smallint``, ``DateTime(timezone=True)`` против ``timestamptz`` — это
  шум без сигнала: расхождение имён типов SQLAlchemy и PostgreSQL здесь норма.
* **``server_default``.** По той же причине, по которой миграция написана руками,
  а не autogenerate: ``array_fill(0, ARRAY[11])`` сравнивается как текст
  выражения и даёт вечный фантомный diff (см. докстринг миграции).

По той же причине здесь нет ``alembic.autogenerate.compare_metadata``: он
спотыкается ровно на том, что перечислено выше, и вдобавок счёл бы расхождением
намеренное различие уникальности в ``reviews`` (см. ``test_index_names_match``).

Каждый тест проверяет один аспект сразу по всем таблицам и собирает все
расхождения, а не падает на первом: при переезде схемы полезнее увидеть список
целиком.
"""

import pytest
from sqlalchemy import CheckConstraint, UniqueConstraint, inspect

from practix_ugc_api.models.base import Base
from practix_ugc_api.models.entity import (  # noqa: F401 — импорт регистрирует таблицы в Base.metadata
    Bookmark,
    FilmRating,
    Like,
    Review,
    ReviewVote,
)

MODEL_TABLES = Base.metadata.tables


@pytest.fixture
async def db_schema(engine):
    """Снимок схемы, накатанной фикстурой ``migrate``: одна интроспекция на тест.

    Интроспекция в SQLAlchemy синхронная, поэтому идёт через ``run_sync`` — и
    целиком внутри одного соединения: ``Inspector`` привязан к соединению и
    после выхода из блока был бы уже непригоден.
    """
    async with engine.connect() as conn:
        return await conn.run_sync(_snapshot)


def _snapshot(sync_conn) -> dict[str, dict]:
    inspector = inspect(sync_conn)
    return {
        table: {
            'columns': inspector.get_columns(table),
            'pk': inspector.get_pk_constraint(table),
            'indexes': inspector.get_indexes(table),
            'checks': inspector.get_check_constraints(table),
        }
        for table in inspector.get_table_names()
    }


def test_every_model_table_is_covered():
    """Страховка от «модель добавили, а в сверку она не попала»."""
    assert set(MODEL_TABLES) == {'likes', 'film_rating', 'bookmarks', 'reviews', 'review_votes'}


async def test_model_tables_all_exist(db_schema):
    missing = set(MODEL_TABLES) - set(db_schema)
    assert not missing, f'таблиц нет в базе: {sorted(missing)} — модель ушла вперёд миграции'


async def test_column_names_match(db_schema):
    def actual(table: str) -> set[str]:
        return {column['name'] for column in db_schema[table]['columns']}

    _assert_no_diff(db_schema, lambda t: set(MODEL_TABLES[t].columns.keys()), actual, 'колонки')


async def test_column_nullability_matches(db_schema):
    problems = {}
    for table in _tables(db_schema):
        expected = {column.name: column.nullable for column in MODEL_TABLES[table].columns}
        found = {column['name']: column['nullable'] for column in db_schema[table]['columns']}
        divergent = {name for name, nullable in expected.items() if found.get(name, nullable) != nullable}
        if divergent:
            problems[table] = sorted(divergent)
    assert not problems, f'NOT NULL в модели и базе разошлись: {problems}'


async def test_primary_key_matches(db_schema):
    problems = {}
    for table in _tables(db_schema):
        # Порядок колонок в составном ключе — часть контракта: (user_id, film_id)
        # обслуживает чтение «оценки пользователя», обратный порядок не обслуживал бы.
        expected = list(MODEL_TABLES[table].primary_key.columns.keys())
        found = db_schema[table]['pk']['constrained_columns']
        if expected != found:
            problems[table] = f'модель {expected}, база {found}'
    assert not problems, f'первичные ключи разошлись: {problems}'


async def test_index_names_match(db_schema):
    def expected(table: str) -> set[str]:
        # UniqueConstraint приплюсован к индексам намеренно: reviews_film_user_uniq
        # объявлен в модели ограничением, а миграцией создан голым CREATE UNIQUE
        # INDEX. В PostgreSQL это разные объекты — get_unique_constraints() его не
        # вернёт, get_indexes() вернёт, — и различие сознательное (см. докстринг
        # models/entity.py). Сравниваем объединение, иначе тест падал бы на том,
        # что задумано.
        model_table = MODEL_TABLES[table]
        return {index.name for index in model_table.indexes} | {
            constraint.name for constraint in model_table.constraints if isinstance(constraint, UniqueConstraint)
        }

    def actual(table: str) -> set[str]:
        return {index['name'] for index in db_schema[table]['indexes']}

    _assert_no_diff(db_schema, expected, actual, 'индексы')


async def test_check_constraint_names_match(db_schema):
    def expected(table: str) -> set[str]:
        return {
            constraint.name for constraint in MODEL_TABLES[table].constraints if isinstance(constraint, CheckConstraint)
        }

    def actual(table: str) -> set[str]:
        return {constraint['name'] for constraint in db_schema[table]['checks']}

    _assert_no_diff(db_schema, expected, actual, 'CHECK-ограничения')


def _tables(db_schema: dict[str, dict]) -> list[str]:
    """Таблицы, которые есть и в модели, и в базе: об остальных сообщает отдельный тест."""
    return sorted(set(MODEL_TABLES) & set(db_schema))


def _assert_no_diff(db_schema, expected, actual, what: str) -> None:
    problems = {}
    for table in _tables(db_schema):
        missing, extra = expected(table) - actual(table), actual(table) - expected(table)
        if missing or extra:
            problems[table] = f'нет в базе: {sorted(missing)}; нет в модели: {sorted(extra)}'
    assert not problems, f'{what} модели и базы разошлись: {problems}'
