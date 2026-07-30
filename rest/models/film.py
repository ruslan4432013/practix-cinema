from typing import Any
from uuid import UUID

from pydantic import BaseModel, model_validator


class FilmShort(BaseModel):
    uuid: UUID
    title: str
    imdb_rating: float | None = None


class FilmPerson(BaseModel):
    uuid: UUID
    full_name: str


class FilmGenre(BaseModel):
    uuid: UUID
    name: str


class FilmDetail(BaseModel):
    uuid: UUID
    title: str
    imdb_rating: float | None = None
    description: str | None = None
    genre: list[FilmGenre] = []
    actors: list[FilmPerson] = []
    writers: list[FilmPerson] = []
    directors: list[FilmPerson] = []


class Genre(BaseModel):
    id: UUID
    name: str


class FilmActorES(BaseModel):
    id: UUID
    full_name: str

    @model_validator(mode='before')
    @classmethod
    def normalize_full_name(cls, data: Any) -> Any:
        if isinstance(data, dict) and 'full_name' not in data and 'name' in data:
            data = dict(data)
            data['full_name'] = data['name']
        return data


class Film(BaseModel):
    id: UUID
    title: str
    description: str | None = None
    rating: float | None = None
    imdb_rating: float | None = None
    type: str = ''
    genres: list[Genre] = []
    actors: list[FilmActorES] = []
    writers: list[FilmActorES] = []
    directors: list[FilmActorES] = []
    access_type: str | None = 'public'

    @model_validator(mode='before')
    @classmethod
    def normalize_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        # rating может быть в поле imdb_rating
        if 'rating' not in data or data['rating'] is None:
            data['rating'] = data.get('imdb_rating')
        return data
