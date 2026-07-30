import sqlite3
from collections.abc import Callable

import psycopg
from psycopg import ClientCursor
from psycopg.rows import dict_row

from sqlite_to_postgres.constant import BATCH_SIZE, ALLOWED_TABLES, PG_Connection, SQLite_Connection, dsl, sqlite_path
from sqlite_to_postgres.errors import NotAllowedTable
from sqlite_to_postgres.mappers import map_entity
from sqlite_to_postgres.mappers import map_genre, map_film_work, map_genre_film_work, map_person, \
    map_person_film_work
from sqlite_to_postgres.postgres_schemas import FilmWorkPostgres, GenrePostgres, GenreFilmWorkPostgres, PersonPostgres, \
    PersonFilmWorkPostgres
from sqlite_to_postgres.shared_schemas import EntityWithDatabase
from sqlite_to_postgres.sqlite_schemas import FilmWorkLite, GenreLite, GenreFilmWorkLite, PersonLite, \
    PersonFilmWorkLite


def test_transfer[LiteEntity: EntityWithDatabase, PSQEntity: EntityWithDatabase](sqlite_connection: SQLite_Connection,
                                                                                 pg_connection: PG_Connection,
                                                                                 sqlite_entity_cls: type[LiteEntity],
                                                                                 postgres_entity_cls: type[PSQEntity],
                                                                                 map_sqlite_to_psql: Callable[
                                                                                     [LiteEntity], PSQEntity]
                                                                                 ):
    table_name = sqlite_entity_cls.__tablename__

    if table_name not in ALLOWED_TABLES:
        raise NotAllowedTable(table_name=table_name)

    sqlite_cursor = sqlite_connection.cursor()
    pg_cursor = pg_connection.cursor()

    sqlite_cursor.execute(f'SELECT * FROM {table_name}')

    while batch := sqlite_cursor.fetchmany(BATCH_SIZE):
        original_entity_batch = [sqlite_entity_cls(**dict(entity)) for entity in batch]
        ids = [entity.id for entity in original_entity_batch]

        pg_cursor.execute(f'SELECT * FROM content.{table_name} WHERE id = ANY(%s)', [ids])
        transferred_entity_batch = [postgres_entity_cls(**pg_entity) for pg_entity in pg_cursor.fetchall()]

        ps = [map_sqlite_to_psql(el) for el in original_entity_batch]

        if map_entity(ps) != map_entity(transferred_entity_batch):
            print(ps)
            print(transferred_entity_batch)

        assert len(original_entity_batch) == len(transferred_entity_batch)
        assert map_entity([map_sqlite_to_psql(el) for el in original_entity_batch]) == map_entity(
            transferred_entity_batch)


def check_consistency(connection: SQLite_Connection, pg_conn: PG_Connection):
    test_transfer(sqlite_connection=connection, pg_connection=pg_conn, sqlite_entity_cls=FilmWorkLite,
                  postgres_entity_cls=FilmWorkPostgres, map_sqlite_to_psql=map_film_work)

    test_transfer(sqlite_connection=connection, pg_connection=pg_conn, sqlite_entity_cls=GenreLite,
                  postgres_entity_cls=GenrePostgres, map_sqlite_to_psql=map_genre)

    test_transfer(sqlite_connection=connection, pg_connection=pg_conn, sqlite_entity_cls=GenreFilmWorkLite,
                  postgres_entity_cls=GenreFilmWorkPostgres, map_sqlite_to_psql=map_genre_film_work)

    test_transfer(sqlite_connection=connection, pg_connection=pg_conn, sqlite_entity_cls=PersonLite,
                  postgres_entity_cls=PersonPostgres, map_sqlite_to_psql=map_person)

    test_transfer(sqlite_connection=connection, pg_connection=pg_conn, sqlite_entity_cls=PersonFilmWorkLite,
                  postgres_entity_cls=PersonFilmWorkPostgres,
                  map_sqlite_to_psql=map_person_film_work)


if __name__ == '__main__':
    with sqlite3.connect(sqlite_path) as sqlite_conn, psycopg.connect(
            **dsl, row_factory=dict_row, cursor_factory=ClientCursor
    ) as pg_conn:
        sqlite_conn.row_factory = sqlite3.Row
        check_consistency(sqlite_conn, pg_conn)
