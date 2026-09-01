"""Схемы ответов выдачи.

ИСТОЧНИК ОТДАЁТСЯ ЯВНО (``source``), и это требование F1.4, а не украшение.
Деградация, о которой не сказано, — это деградация, которую никто не заметит:
фронт не сможет подписать блок («Популярное» вместо «С этим смотрят»), а в
метриках подмена персональной выдачи популярной будет выглядеть здоровьем.

``version`` — номер версии витрины, из которой собран ответ. По нему видно, что
все позиции пришли из одного батча, и он же помогает в разборе инцидента:
«выдача старая» и «выдача пустая» — разные аварии.
"""

import enum
import uuid

from pydantic import BaseModel, Field


class RecommendationSource(enum.StrEnum):
    """Откуда на самом деле пришли позиции ответа."""

    SIMILAR = 'similar'
    PERSONAL = 'personal'
    POPULAR = 'popular'


class RecommendationItem(BaseModel):
    """Одна позиция блока.

    Идентификатор и вес — всё, что витрина хранит. ``title`` и ``imdb_rating``
    появляются, только если вызывающий попросил ``enrich=true``: каталог
    принадлежит Movies API, и дублировать его здесь мы не будем.
    """

    film_id: uuid.UUID = Field(description='Идентификатор фильма в каталоге')
    score: float = Field(description='Вес позиции; порядок уже отсортирован, пересортировывать не нужно')
    title: str | None = Field(default=None, description='Заполняется только при enrich=true, из Movies API')
    imdb_rating: float | None = Field(default=None, description='Заполняется только при enrich=true')


class RecommendationsResponse(BaseModel):
    source: RecommendationSource = Field(
        description='Что реально отдано: similar / personal / popular. popular — это путь деградации'
    )
    version: int | None = Field(default=None, description='Версия витрины; null — обучение ещё не проходило')
    items: list[RecommendationItem] = Field(description='Позиции блока по убыванию близости')
