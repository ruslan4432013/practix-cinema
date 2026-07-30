import sqlite3
from pathlib import Path

import psycopg

from sqlite_to_postgres.sqlite_schemas import FilmWorkLite, GenreFilmWorkLite, GenreLite, PersonFilmWorkLite, PersonLite

BATCH_SIZE = 100

ALLOWED_ENTITIES = [FilmWorkLite, GenreLite, GenreFilmWorkLite, PersonLite, PersonFilmWorkLite]

ALLOWED_TABLES = [entity.__tablename__ for entity in ALLOWED_ENTITIES]

type PG_Connection = psycopg.connection.Connection

type SQLite_Connection = sqlite3.Connection

dsl = {'dbname': 'movies_database', 'user': 'app', 'password': '123qwe', 'host': '127.0.0.1', 'port': 5432}

current_file = Path(__file__).resolve()

sqlite_path = current_file.parent / 'db.sqlite'
