"""Чтение витрины из PostgreSQL — источник истины, второй по счёту.

Порядок обращения задан ADR-004: сначала горячий слой (``services/cache.py``),
сюда попадаем на промахе или при недоступном Redis. Здесь нет ни одной строчки
про деградацию — этим занимается ``services/degradation.py``; задача этого
модуля ровно одна: достать строки версии.

ВСЕ ЧТЕНИЯ ПАРАМЕТРИЗОВАНЫ НОМЕРОМ ВЕРСИИ, и он берётся один раз на запрос.
Это и есть F0.2: выдача видит целостный батч. Если бы каждый запрос читал
«последнюю» версию сам по себе, между чтением популярного и чтением похожих
батч мог бы переключиться, и ответ собрался бы из двух разных моделей.
"""

import datetime
import uuid
from dataclasses import dataclass

from sqlalchemy import literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from practix_recommendations_api.models.entity import (
    STATUS_READY,
    CatalogFilm,
    PersonalItem,
    PopularItem,
    ShelfPointer,
    ShelfVersion,
    SimilarItem,
)


@dataclass(frozen=True, slots=True)
class ShelfState:
    """Что сейчас показывает указатель.

    ``version is None`` — законное состояние, а не ошибка: стенд поднялся,
    обучение ещё не проходило. Выдача в этот момент обязана отвечать 200 с
    пустым популярным (F1.3), а не 500.
    """

    version: int | None
    finished_at: datetime.datetime | None
    models: str

    @property
    def age_seconds(self) -> float | None:
        """Возраст витрины. ``None``, пока обучение не завершалось ни разу."""
        if self.finished_at is None:
            return None
        now = datetime.datetime.now(datetime.UTC)
        # Метка может прийти naive, если кто-то писал витрину в обход миграции;
        # считать разницу между naive и aware — это TypeError на горячем пути.
        finished = self.finished_at
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=datetime.UTC)
        return (now - finished).total_seconds()


EMPTY_SHELF = ShelfState(version=None, finished_at=None, models='')


class ShelfReader:
    """Только чтение. Писателем витрины является ровно один процесс — батч."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_state(self) -> ShelfState:
        """Актуальная версия витрины и когда она была разложена."""
        stmt = (
            select(ShelfVersion.version, ShelfVersion.finished_at, ShelfVersion.models)
            .join(ShelfPointer, ShelfPointer.version == ShelfVersion.version)
            # Указатель переставляется только на готовую версию, но проверка
            # здесь стоит намеренно: она делает инвариант читаемым в месте
            # чтения, а не только в месте записи.
            .where(ShelfVersion.status == STATUS_READY)
        )
        row = (await self._db.execute(stmt)).first()
        if row is None:
            return EMPTY_SHELF
        return ShelfState(version=row.version, finished_at=row.finished_at, models=row.models)

    async def similar(self, version: int, film_id: uuid.UUID, limit: int) -> list[tuple[uuid.UUID, float]]:
        """Соседи фильма по со-встречаемости, в порядке, посчитанном батчем."""
        stmt = (
            select(SimilarItem.rec_film_id, SimilarItem.score)
            .where(SimilarItem.version == version, SimilarItem.film_id == film_id)
            .order_by(SimilarItem.rank)
            .limit(limit)
        )
        return [(row.rec_film_id, row.score) for row in (await self._db.execute(stmt)).all()]

    async def personal(self, version: int, user_id: uuid.UUID, limit: int) -> list[tuple[uuid.UUID, float]]:
        """Персональная выдача. Просмотренное исключено ещё батчем (F4.3)."""
        stmt = (
            select(PersonalItem.film_id, PersonalItem.score)
            .where(PersonalItem.version == version, PersonalItem.user_id == user_id)
            .order_by(PersonalItem.rank)
            .limit(limit)
        )
        return [(row.film_id, row.score) for row in (await self._db.execute(stmt)).all()]

    async def popular(self, version: int, limit: int) -> list[tuple[uuid.UUID, float]]:
        """Топ за окно — он же путь деградации для двух предыдущих."""
        stmt = (
            select(PopularItem.film_id, PopularItem.score)
            .where(PopularItem.version == version)
            .order_by(PopularItem.rank)
            .limit(limit)
        )
        return [(row.film_id, row.score) for row in (await self._db.execute(stmt)).all()]

    async def catalog_lookup(self, version: int, film_id: uuid.UUID) -> tuple[bool, bool]:
        """Возвращает пару «фильм найден» и «снимок каталога этой версии вообще есть».

        Второй флаг существует ради единственного правила: 404 отдаётся, только
        когда отсутствие фильма ДОКАЗАНО. Пустой снимок каталога доказывает не
        отсутствие фильма, а отсутствие снимка — например, указатель в кэше
        пережил свою версию, или каталог был недоступен в момент обучения. Без
        этого различия устаревший указатель превращал бы в 404 весь каталог
        разом: ни одного фильма в несуществующей версии нет по построению.

        Один запрос, а не два: развилка нужна только на промахе (соседей не
        нашлось), но и там платить двумя round-trip незачем.
        """
        stmt = select(
            select(literal(1))
            .where(CatalogFilm.version == version, CatalogFilm.film_id == film_id)
            .exists()
            .label('found'),
            select(literal(1)).where(CatalogFilm.version == version).exists().label('loaded'),
        )
        row = (await self._db.execute(stmt)).one()
        return bool(row.found), bool(row.loaded)
