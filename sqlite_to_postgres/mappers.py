from sqlite_to_postgres.postgres_schemas import (
    FilmWorkPostgres,
    GenreFilmWorkPostgres,
    GenrePostgres,
    PersonFilmWorkPostgres,
    PersonPostgres,
)
from sqlite_to_postgres.shared_schemas import EntityWithDatabase
from sqlite_to_postgres.sqlite_schemas import FilmWorkLite, GenreFilmWorkLite, GenreLite, PersonFilmWorkLite, PersonLite


def map_film_work(film_work_sqlite: FilmWorkLite) -> FilmWorkPostgres:
    return FilmWorkPostgres(
        id=film_work_sqlite.id,
        title=film_work_sqlite.title,
        description=film_work_sqlite.description,
        creation_date=film_work_sqlite.creation_date,
        rating=film_work_sqlite.rating,
        type=film_work_sqlite.type,
        created=film_work_sqlite.created_at,
        modified=film_work_sqlite.updated_at,
    )


def map_genre(genre_sqlite: GenreLite) -> GenrePostgres:
    return GenrePostgres(
        id=genre_sqlite.id,
        description=genre_sqlite.description,
        created=genre_sqlite.created_at,
        modified=genre_sqlite.updated_at,
        name=genre_sqlite.name,
    )


def map_genre_film_work(genre_film_work_sqlite: GenreFilmWorkLite) -> GenreFilmWorkPostgres:
    return GenreFilmWorkPostgres(
        id=genre_film_work_sqlite.id,
        created=genre_film_work_sqlite.created_at,
        genre_id=genre_film_work_sqlite.genre_id,
        film_work_id=genre_film_work_sqlite.film_work_id,
    )


def map_person(person_sqlite: PersonLite) -> PersonPostgres:
    return PersonPostgres(
        id=person_sqlite.id,
        full_name=person_sqlite.full_name,
        created=person_sqlite.created_at,
        modified=person_sqlite.updated_at,
    )


def map_person_film_work(person_film_work_sqlite: PersonFilmWorkLite) -> PersonFilmWorkPostgres:
    return PersonFilmWorkPostgres(
        id=person_film_work_sqlite.id,
        created=person_film_work_sqlite.created_at,
        role=person_film_work_sqlite.role,
        film_work_id=person_film_work_sqlite.film_work_id,
        person_id=person_film_work_sqlite.person_id,
    )


def map_entity[E: EntityWithDatabase](entities: list[E]) -> dict[str, E]:
    result: dict[str, E] = {}

    for entity in entities:
        result[str(entity.id)] = entity

    return result
