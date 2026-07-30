import dataclasses
import datetime
from uuid import UUID

from sqlite_to_postgres.shared_schemas import EntityWithDatabase


@dataclasses.dataclass
class FilmWorkLite(EntityWithDatabase):
    __tablename__ = 'film_work'
    id: UUID
    title: str
    description: str
    creation_date: datetime.date | None
    file_path: str
    rating: float
    type: str
    created_at: datetime.datetime
    updated_at: datetime.datetime

    def __post_init__(self):
        if isinstance(self.id, str):
            self.id = UUID(self.id)


@dataclasses.dataclass
class GenreLite(EntityWithDatabase):
    __tablename__ = 'genre'
    id: UUID
    name: str
    description: str | None
    created_at: datetime.datetime
    updated_at: datetime.datetime

    def __post_init__(self):
        if isinstance(self.id, str):
            self.id = UUID(self.id)


@dataclasses.dataclass
class GenreFilmWorkLite(EntityWithDatabase):
    __tablename__ = 'genre_film_work'
    id: UUID
    film_work_id: str
    genre_id: str
    created_at: datetime.datetime

    def __post_init__(self):
        if isinstance(self.id, str):
            self.id = UUID(self.id)


@dataclasses.dataclass
class PersonLite(EntityWithDatabase):
    __tablename__ = 'person'
    id: UUID
    full_name: str
    created_at: datetime.datetime
    updated_at: datetime.datetime

    def __post_init__(self):
        if isinstance(self.id, str):
            self.id = UUID(self.id)


@dataclasses.dataclass
class PersonFilmWorkLite(EntityWithDatabase):
    __tablename__ = 'person_film_work'
    id: str
    film_work_id: str
    person_id: str
    role: str
    created_at: datetime.datetime
