"""Схема данных UGC.

ИСТОЧНИК ПРАВДЫ — МИГРАЦИЯ, А НЕ ЭТОТ ФАЙЛ. Таблицы создаёт ручной DDL в
``migrations/versions/0001_initial_ugc_schema.py``; ``Base.metadata`` здесь
никогда не разворачивает схему (``create_all`` не вызывается нигде, включая
тесты) и нужна для типизации и для чтения человеком. Значит менять схему нужно
миграцией, а модель править следом — иначе рефакторинг «по моделям» разъедется
с реальной базой молча. Расхождение ловит ``tests/functional/test_schema.py``:
он сверяет модели с тем, что накатила миграция.

Повторяет — дословно, включая имена индексов — схему, на которой в
``research/ugc-storage/`` мерились 11 сценариев на 10 млн оценок. Расхождение
модели с измеренной схемой обесценило бы замеры, поэтому индексы объявлены
явными именами, а не отданы на откуп автогенерации.

Единственное ДОБАВЛЕНИЕ к измеренной схеме — уникальность ``(film_id, user_id)``
в ``reviews``: продуктовое правило «одна рецензия пользователя на фильм». В
стенде его не было, потому что там рецензии только заливались пачками и
конкуренции за уникальность не возникало.

Пользователей сервис не хранит: ``user_id`` приходит из подписи JWT, внешних
ключей на таблицы Auth нет и быть не может — это разные базы разных сервисов.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from practix_ugc_api.models.base import Base


class Like(Base):
    """Оценка фильма пользователем.

    «Лайк» и «дизлайк» — частные случаи оценки 10 и 0, отдельных сущностей нет.
    Диапазон 0..10 закрыт типом (``smallint``) и ограничением, а не только
    валидацией в приложении: база — последний рубеж, который переживёт и ошибку
    в коде, и ручной ``INSERT`` при разборе инцидента.
    """

    __tablename__ = 'likes'

    # Первичный ключ (user_id, film_id) — это и есть требование «один
    # пользователь ставит фильму одну оценку». Он же обслуживает чтение
    # «оценки пользователя».
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    film_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint('rating BETWEEN 0 AND 10', name='likes_rating_check'),
        # Второй паттерн доступа — «по фильму». Без отдельного индекса он
        # превращается в seq scan по 10 млн строк.
        Index('likes_film_rating_idx', 'film_id', 'rating'),
        # Правило ESR: равенство по user_id, дальше поля в порядке сортировки.
        # Первичный ключ выборку «понравившееся» не закрывает — по нему пришлось
        # бы сортировать вручную.
        Index('likes_user_rating_idx', 'user_id', text('rating DESC'), text('updated_at DESC')),
    )


class FilmRating(Base):
    """Преагрегат рейтинга фильма.

    ``hist[i]`` — число оценок со значением ``i - 1`` (массивы в PostgreSQL
    1-индексные). Гистограмма, а не пара счётчиков «лайки/дизлайки»: порог, с
    которого оценка считается лайком, — продуктовое решение и будет меняться.
    Из 11 чисел любой порог считается за O(1), а два счётчика пришлось бы
    пересчитывать по всей таблице.

    Смысл таблицы — разница между «посчитать на лету» и «прочитать посчитанное»:
    на замерах это 13× у PostgreSQL и единственное место, где 200 мс реально
    могли не выдержать.
    """

    __tablename__ = 'film_rating'

    film_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    ratings_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text('0'))
    ratings_sum: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text('0'))
    hist: Mapped[list[int]] = mapped_column(
        ARRAY(Integer),
        nullable=False,
        server_default=text('array_fill(0, ARRAY[11])'),
    )


class Bookmark(Base):
    """Закладка «посмотреть позже». Сортировок, кроме «свежие сверху», не требуется."""

    __tablename__ = 'bookmarks'

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    film_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (Index('bookmarks_user_created_idx', 'user_id', text('created_at DESC')),)


class Review(Base):
    """Рецензия на фильм.

    ``votes_*`` и ``useful_score`` денормализованы в саму рецензию: сортировка
    списка по полезности не должна джойнить таблицу голосов на два миллиона
    строк ради двадцати строк вывода.
    """

    __tablename__ = 'reviews'

    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    film_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    author_rating: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    votes_likes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text('0'))
    votes_dislikes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text('0'))
    useful_score: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text('0'))

    __table_args__ = (
        CheckConstraint('author_rating BETWEEN 0 AND 10', name='reviews_author_rating_check'),
        UniqueConstraint('film_id', 'user_id', name='reviews_film_user_uniq'),
        # По индексу на каждый поддерживаемый порядок сортировки. Алгоритм
        # ранжирования ещё будет меняться, и новый порядок стоит одного индекса,
        # а не переезда схемы.
        Index('reviews_film_created_idx', 'film_id', text('created_at DESC')),
        Index('reviews_film_useful_idx', 'film_id', text('useful_score DESC')),
        # NULLS LAST — единственное отступление от DDL стенда, и оно вынужденное:
        # там author_rating был заполнен всегда, а здесь поле необязательное, и
        # PostgreSQL при DESC ставит NULL первыми — рецензии без оценки автора
        # возглавили бы список «по оценке». Порядок в индексе обязан совпадать с
        # порядком в запросе, иначе индекс перестанет использоваться.
        Index('reviews_film_author_rating_idx', 'film_id', text('author_rating DESC NULLS LAST')),
    )


class ReviewVote(Base):
    """Голос за рецензию: +1 «полезно», -1 «бесполезно».

    Существует ради идемпотентности: без строки на голосующего повторный клик
    накручивал бы счётчик. Сами счётчики двигаются дельтой в ``reviews``.
    """

    __tablename__ = 'review_votes'

    review_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    value: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (CheckConstraint('value IN (-1, 1)', name='review_votes_value_check'),)
