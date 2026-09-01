"""ORM-модели витрины рекомендаций.

Модели ЗЕРКАЛЯТ ручной DDL из ``migrations/versions/0001_initial_recs_schema.py``,
а не наоборот: миграция — источник истины, включая имена индексов и
CHECK-ограничения, которых autogenerate не видит. Тот же порядок принят в
``ugc-api`` и ``link-shortener``.

Схемой витрины владеет ИМЕННО ЭТОТ сервис, хотя пишет в неё батч
``recsys-trainer``. Причина в compose: контейнер миграций переиспользует образ
сервиса, а из двух — выдача живёт в ядре и поднимается всегда, тогда как батч
сидит в профиле ``warehouse`` и его образа на голом ядре может не быть вовсе.
Батч пишет сюда сырым SQL (пакетная вставка), поэтому второй копии этих
классов в репозитории нет.

ВЕРСИЯ ВХОДИТ В ПЕРВИЧНЫЙ КЛЮЧ каждой из трёх таблиц списков. Из этого
следуют оба системных требования разом: повторный прогон физически не может
удвоить строки (F0.1), а выдача, прочитавшая номер версии, видит целостный
батч и никогда — половину нового поверх половины старого (F0.2).
"""

import datetime
import uuid

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from practix_recommendations_api.models.base import Base

# Статусы версии витрины. 'building' виден выдаче ровно никогда: указатель
# переставляется только на 'ready'.
STATUS_BUILDING = 'building'
STATUS_READY = 'ready'
STATUS_FAILED = 'failed'

MODEL_COOCCURRENCE = 'cooccurrence'
MODEL_ALS = 'als'
MODEL_POPULAR = 'popular'


class ShelfVersion(Base):
    """Одна строка на прогон обучения."""

    __tablename__ = 'shelf_version'

    version: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # Естественный ключ прогона: 'train:2026-08-30T00'. Уникальность — это то,
    # что стоит между двумя тиками планировщика (или двумя репликами) и второй
    # витриной на тех же данных. Тот же приём, что у scheduled_run.run_key в
    # нотификациях.
    run_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text(f"'{STATUS_BUILDING}'"))
    # Какие модели вошли в версию: 'cooccurrence+als'. Свободная строка, а не
    # enum: набор моделей меняется чаще, чем схема (E6 режется целиком).
    models: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("''"))
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text('now()')
    )
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Размеры витрины и метрики качества (E5) — тем же прогоном, что их посчитал.
    stats: Mapped[dict] = mapped_column(
        JSONB().with_variant(JSON(), 'sqlite'), nullable=False, server_default=text("'{}'")
    )

    __table_args__ = (
        CheckConstraint(
            f"status IN ('{STATUS_BUILDING}', '{STATUS_READY}', '{STATUS_FAILED}')",
            name='shelf_version_status_check',
        ),
        Index('shelf_version_finished_idx', 'finished_at'),
    )


class ShelfPointer(Base):
    """Указатель на актуальную версию. Строка ровно одна — это гарантия схемы.

    ``id boolean PRIMARY KEY CHECK (id)`` — приём, который делает «ровно одну
    строку» свойством таблицы, а не договорённостью: второй ``INSERT`` упрётся
    в первичный ключ, а ``id = false`` не пройдёт CHECK. Альтернатива
    «договоримся всегда писать WHERE id = 1» держится ровно до первой ошибки.
    """

    __tablename__ = 'shelf_pointer'

    id: Mapped[bool] = mapped_column(Boolean, primary_key=True, server_default=text('true'))
    version: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey('shelf_version.version', ondelete='RESTRICT'), nullable=True
    )
    switched_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (CheckConstraint('id', name='shelf_pointer_single_row_check'),)


class SimilarItem(Base):
    """«С этим смотрят»: соседи фильма по со-встречаемости в просмотрах."""

    __tablename__ = 'similar_item'

    version: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    film_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    rank: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    rec_film_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)


class PersonalItem(Base):
    """«Рекомендуем вам»: персональная выдача, посчитанная ALS."""

    __tablename__ = 'personal_item'

    version: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    rank: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    film_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)


class PopularItem(Base):
    """Путь деградации: топ за окно. Одна строка на позицию, версия та же."""

    __tablename__ = 'popular_item'

    version: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    rank: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    film_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)


class CatalogFilm(Base):
    """Список идентификаторов каталога на момент обучения.

    Нужен ровно для двух вещей, и обе — требования ТЗ: отличить «фильма нет»
    (404 по F2.4) от «фильм есть, соседей нет» (200 с популярным по F1.3), и
    посчитать покрытие каталога (SLO ≥ 60%).

    Каталог этим НЕ дублируется: здесь только идентификатор и версия, ни
    названия, ни жанров, ни описания. Карточку по-прежнему отдаёт Movies API —
    иначе у каталога появился бы второй владелец.
    """

    __tablename__ = 'catalog_film'

    version: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    film_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
