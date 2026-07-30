import dataclasses
import sqlite3
from collections.abc import Callable, Generator

import psycopg
from psycopg import ClientCursor
from psycopg.rows import dict_row

from sqlite_to_postgres.constant import ALLOWED_TABLES, BATCH_SIZE, PG_Connection, SQLite_Connection, dsl, sqlite_path
from sqlite_to_postgres.errors import NotAllowedTable
from sqlite_to_postgres.mappers import map_film_work, map_genre, map_genre_film_work, map_person, map_person_film_work
from sqlite_to_postgres.shared_schemas import EntityWithDatabase
from sqlite_to_postgres.sqlite_schemas import FilmWorkLite, GenreFilmWorkLite, GenreLite, PersonFilmWorkLite, PersonLite


class PostgresSaver:
    def __init__(self, pg_connection: PG_Connection):
        self.pg_connection = pg_connection

    def save_entities[T: EntityWithDatabase](self, entities: list[T]):
        if not entities:
            raise Exception('Empty entities in save_entities')
        table_name = entities[0].__tablename__

        if table_name not in ALLOWED_TABLES:
            raise NotAllowedTable(table_name)

        meta_entity = entities[0]

        column_names = [field.name for field in dataclasses.fields(meta_entity)]
        column_names_str = ','.join(column_names)
        col_count = ', '.join(['%s'] * len(column_names))

        pg_cursor = self.pg_connection.cursor()

        query = (
            f'INSERT INTO content.{table_name} ({column_names_str}) VALUES ({col_count}) ON CONFLICT (id) DO NOTHING'
        )

        batch_as_tuples = [dataclasses.astuple(entity) for entity in entities]

        pg_cursor.executemany(query, batch_as_tuples)

    def load_entity[T: EntityWithDatabase](self, entity: type[T]) -> list[T]:
        table_name = entity.__tablename__

        if table_name not in ALLOWED_TABLES:
            raise NotAllowedTable(table_name)

        pg_cursor = self.pg_connection.cursor()

        pg_cursor.execute(f'SELECT * from content.{table_name}')

        return [entity(**student) for student in pg_cursor.fetchall()]


class SQLiteLoader:
    def __init__(self, sqlite_connection: SQLite_Connection):
        self.sqlite_connection = sqlite_connection
        self.sqlite_connection.row_factory = sqlite3.Row

    def load_entity[Entity, Result = Entity](
        self, entity_cls: type[Entity], mapper: Callable[[Entity], Result]
    ) -> Generator[list[Result]]:
        table_name = entity_cls.__tablename__
        if table_name not in ALLOWED_TABLES:
            raise NotAllowedTable(table_name=table_name)

        for batch in self._transform_data(entity_cls):
            yield [mapper(el) for el in batch]

    def _extract_data[Entity: EntityWithDatabase](self, entity_cls: type[Entity]) -> Generator[list[sqlite3.Row]]:
        table_name = entity_cls.__tablename__

        if table_name not in ALLOWED_TABLES:
            raise NotAllowedTable(table_name=table_name)

        sqlite_cursor = self.sqlite_connection.cursor()
        sqlite_cursor.execute(f'SELECT * FROM {table_name}')
        while results := sqlite_cursor.fetchmany(BATCH_SIZE):
            yield results

    def _transform_data[Entity: EntityWithDatabase](self, entity_cls: type[Entity]) -> Generator[list[Entity]]:
        for batch in self._extract_data(entity_cls):
            # тут можно было бы, при необходимости, обработать полученные данные
            yield [entity_cls(**dict(value)) for value in batch]


def load_from_sqlite(connection: SQLite_Connection, pg_conn: PG_Connection):
    """Основной метод загрузки данных из SQLite в Postgres"""
    postgres_saver = PostgresSaver(pg_conn)
    sqlite_loader = SQLiteLoader(connection)

    for film_works in sqlite_loader.load_entity(FilmWorkLite, map_film_work):
        postgres_saver.save_entities(film_works)

    for genres in sqlite_loader.load_entity(GenreLite, map_genre):
        postgres_saver.save_entities(genres)

    for genre_film_works in sqlite_loader.load_entity(GenreFilmWorkLite, map_genre_film_work):
        postgres_saver.save_entities(genre_film_works)

    for person in sqlite_loader.load_entity(PersonLite, map_person):
        postgres_saver.save_entities(person)

    for person_film_works in sqlite_loader.load_entity(PersonFilmWorkLite, map_person_film_work):
        postgres_saver.save_entities(person_film_works)


if __name__ == '__main__':
    with (
        sqlite3.connect(sqlite_path) as sqlite_conn,
        psycopg.connect(**dsl, row_factory=dict_row, cursor_factory=ClientCursor) as pg_conn,
    ):
        load_from_sqlite(sqlite_conn, pg_conn)
