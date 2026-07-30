import dataclasses
import datetime
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlite_to_postgres.shared_schemas import EntityWithDatabase


@dataclasses.dataclass
class FilmWorkPostgres(EntityWithDatabase):
    __tablename__ = "film_work"
    id: UUID
    title: str
    description: str
    creation_date: datetime.date | None
    rating: float
    type: str
    created: datetime.datetime
    modified: datetime.datetime

    def __post_init__(self):
        if isinstance(self.id, str):
            self.id = UUID(self.id)

        if isinstance(self.rating, Decimal):
            self.rating = float(self.rating)

        if isinstance(self.created, str):
            self.created = datetime.datetime.fromisoformat(self.created).astimezone(ZoneInfo('Etc/UTC'))

        if isinstance(self.modified, str):
            self.modified = datetime.datetime.fromisoformat(self.modified).astimezone(ZoneInfo('Etc/UTC'))


@dataclasses.dataclass
class GenrePostgres(EntityWithDatabase):
    __tablename__ = "genre"
    id: UUID
    name: str
    description: str | None
    created: datetime.datetime
    modified: datetime.datetime

    def __post_init__(self):
        if isinstance(self.id, str):
            self.id = UUID(self.id)

        if isinstance(self.created, str):
            self.created = datetime.datetime.fromisoformat(self.created).astimezone(ZoneInfo('Etc/UTC'))

        if isinstance(self.modified, str):
            self.modified = datetime.datetime.fromisoformat(self.modified).astimezone(ZoneInfo('Etc/UTC'))


@dataclasses.dataclass
class GenreFilmWorkPostgres(EntityWithDatabase):
    __tablename__ = "genre_film_work"
    id: UUID
    film_work_id: UUID
    genre_id: UUID
    created: datetime.datetime

    def __post_init__(self):
        if isinstance(self.id, str):
            self.id = UUID(self.id)

        if isinstance(self.film_work_id, str):
            self.film_work_id = UUID(self.film_work_id)

        if isinstance(self.genre_id, str):
            self.genre_id = UUID(self.genre_id)

        if isinstance(self.created, str):
            self.created = datetime.datetime.fromisoformat(self.created).astimezone(ZoneInfo('Etc/UTC'))


@dataclasses.dataclass
class PersonPostgres(EntityWithDatabase):
    __tablename__ = "person"
    id: UUID
    full_name: str
    created: datetime.datetime
    modified: datetime.datetime

    def __post_init__(self):
        if isinstance(self.id, str):
            self.id = UUID(self.id)

        if isinstance(self.created, str):
            self.created = datetime.datetime.fromisoformat(self.created).astimezone(ZoneInfo('Etc/UTC'))

        if isinstance(self.modified, str):
            self.modified = datetime.datetime.fromisoformat(self.modified).astimezone(ZoneInfo('Etc/UTC'))


@dataclasses.dataclass
class PersonFilmWorkPostgres(EntityWithDatabase):
    __tablename__ = "person_film_work"
    id: UUID
    film_work_id: UUID
    person_id: UUID
    role: str
    created: datetime.datetime

    def __post_init__(self):
        if isinstance(self.id, str):
            self.id = UUID(self.id)

        if isinstance(self.film_work_id, str):
            self.film_work_id = UUID(self.film_work_id)

        if isinstance(self.person_id, str):
            self.person_id = UUID(self.person_id)

        if isinstance(self.created, str):
            self.created = datetime.datetime.fromisoformat(self.created).astimezone(ZoneInfo('Etc/UTC'))
