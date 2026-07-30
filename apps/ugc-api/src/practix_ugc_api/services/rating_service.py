"""Оценки фильмов и преагрегат рейтинга.

Запросы записаны на голом SQL, а не собраны ORM, потому что это ТЕ САМЫЕ
запросы, на которых в ``research/ugc-storage/`` получены цифры: upsert с CTE,
возвращающим прошлую оценку, и поэлементное сложение гистограммы через
``generate_series``. Собранный ORM эквивалент дал бы другой план и обесценил бы
замеры.

ОТЛИЧИЕ ОТ СТЕНДА — транзакция и блокировка. В стенде оба запроса шли на
autocommit двумя независимыми транзакциями, и одну и ту же пару
``(user_id, film_id)`` он никогда не писал параллельно. Для замера задержки это
неважно, для сервиса — нет:

1. **Атомарность.** Оценка и преагрегат пишутся в ОДНОЙ транзакции. Иначе сбой
   между двумя коммитами оставляет оценку записанной, а преагрегат — устаревшим
   навсегда и незаметно. Сервис обещает, что ответ «сохранено» означает
   «сохранено», а не «сохранено наполовину».

2. **Два состязания на одной паре (пользователь, фильм),** которых стенд не
   доставал, и оба ломают преагрегат необратимо:

   * *Строка уже есть.* На READ COMMITTED подзапрос ``previous`` внутри
     ``_UPSERT_LIKE`` читает снимок НАЧАЛА оператора, а ``ON CONFLICT DO UPDATE``
     в том же операторе ждёт блокировку строки и меняет уже НОВУЮ версию. Двойной
     клик «5 → 8» и «5 → 3» даёт второй транзакции ``old_rating = 5``, хотя на
     деле там уже 8: сумма разъезжается, а в корзине гистограммы 8 навсегда
     остаётся фантомная единица.
   * *Строки ещё нет.* Обе транзакции видят «прошлой оценки нет» и обе прибавляют
     ``+1`` к количеству. У одного пользователя на одном фильме получается две
     оценки, и ``sum(hist)`` перестаёт сходиться с ``ratings_count``. Блокировкой
     строки это не лечится: блокировать нечего.

   Оба закрывает ``pg_advisory_xact_lock`` по паре (пользователь, фильм):
   ключ существует независимо от наличия строки, блокировка снимается на коммите
   сама, а разные пользователи и разные фильмы друг друга не ждут. Блокировать
   строку ``film_rating`` было бы проще, но это выстроило бы в очередь ВСЕХ
   оценивающих популярный фильм — то есть ровно тех, кого нельзя.

   Разным пользователям одного фильма блокировка и не нужна: преагрегат меняется
   не «прочитал — посчитал — записал», а ``+= дельта`` внутри одного оператора,
   где блокировка строки берётся самим PostgreSQL, а проигравший после
   разблокировки читает уже закоммиченную версию и добавляет своё поверх.
"""

import logging
import uuid

from sqlalchemy import ARRAY, Integer, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from practix_ugc_api.core.config import settings
from practix_ugc_api.models.schemas import FilmRatingResponse, LikedFilm
from practix_ugc_api.services.aggregate import HISTOGRAM_SIZE, RatingDelta, average, rating_deltas, split_by_threshold

logger = logging.getLogger(__name__)

# Ключ блокировки строится из пары идентификаторов, а не из строки таблицы:
# он обязан существовать и тогда, когда оценки ещё нет.
_ADVISORY_LOCK = text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))')

# Прошлое значение оценки нужно, чтобы поправить преагрегат, а ON CONFLICT
# возвращает только новую строку. CTE решает это за один round trip.
_UPSERT_LIKE = text(
    """
    WITH previous AS (
        SELECT rating FROM likes WHERE user_id = :user_id AND film_id = :film_id
    ), upserted AS (
        INSERT INTO likes (user_id, film_id, rating, created_at, updated_at)
        VALUES (:user_id, :film_id, :rating, now(), now())
        ON CONFLICT (user_id, film_id)
        DO UPDATE SET rating = EXCLUDED.rating, updated_at = now()
        RETURNING 1
    )
    SELECT (SELECT rating FROM previous) AS old_rating
    """
)

_DELETE_LIKE = text('DELETE FROM likes WHERE user_id = :user_id AND film_id = :film_id RETURNING rating')

# Гистограмма складывается поэлементно через generate_series, а не через
# многоаргументный unnest: индексация по позиции читается однозначно и не
# зависит от того, в каком порядке unnest отдаст строки.
_HISTOGRAM_SUM = """
    SELECT array_agg(film_rating.hist[i] + (CAST(:d_hist AS integer[]))[i] ORDER BY i)
    FROM generate_series(1, :hist_size) AS i
"""

# Форма с ON CONFLICT — для постановки и изменения оценки: строки преагрегата
# может ещё не быть, и её нужно создать.
_APPLY_RATING_DELTA = text(
    f"""
    INSERT INTO film_rating (film_id, ratings_count, ratings_sum, hist)
    VALUES (:film_id, :d_count, :d_sum, CAST(:d_hist AS integer[]))
    ON CONFLICT (film_id) DO UPDATE SET
        ratings_count = film_rating.ratings_count + :d_count,
        ratings_sum   = film_rating.ratings_sum + :d_sum,
        hist          = ({_HISTOGRAM_SUM})
    RETURNING ratings_count, ratings_sum, hist
    """
).bindparams(bindparam('d_hist', type_=ARRAY(Integer)))

# Форма без ON CONFLICT — для снятия оценки. Разница не косметическая: у
# отрицательной дельты ветка INSERT создала бы строку с ratings_count = -1.
# Обновление нуля строк — честный исход, его видно в логе.
_SUBTRACT_RATING_DELTA = text(
    f"""
    UPDATE film_rating SET
        ratings_count = ratings_count + :d_count,
        ratings_sum   = ratings_sum + :d_sum,
        hist          = ({_HISTOGRAM_SUM})
    WHERE film_id = :film_id
    RETURNING ratings_count, ratings_sum, hist
    """
).bindparams(bindparam('d_hist', type_=ARRAY(Integer)))

_SELECT_FILM_RATING = text('SELECT ratings_count, ratings_sum, hist FROM film_rating WHERE film_id = :film_id')

# Сценарий R1 исследования. Индекс likes_user_rating_idx построен ровно под этот
# порядок сортировки.
_SELECT_USER_LIKES = text(
    """
    SELECT film_id, rating, updated_at
    FROM likes
    WHERE user_id = :user_id AND rating >= :min_rating
    ORDER BY rating DESC, updated_at DESC
    LIMIT :limit OFFSET :offset
    """
)

_EMPTY_HISTOGRAM = [0] * HISTOGRAM_SIZE


def build_film_rating(
    film_id: uuid.UUID, ratings_count: int, ratings_sum: int, histogram: list[int]
) -> FilmRatingResponse:
    """Собирает ответ по строке преагрегата, выводя лайки и дизлайки из гистограммы."""
    likes, dislikes = split_by_threshold(histogram, settings.UGC_API_LIKE_THRESHOLD)
    return FilmRatingResponse(
        film_id=film_id,
        ratings_count=ratings_count,
        average=average(ratings_sum, ratings_count),
        likes=likes,
        dislikes=dislikes,
        like_threshold=settings.UGC_API_LIKE_THRESHOLD,
        histogram=histogram,
    )


class RatingService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def set_rating(self, user_id: uuid.UUID, film_id: uuid.UUID, rating: int) -> FilmRatingResponse:
        """Ставит или изменяет оценку и возвращает агрегат фильма уже с её учётом."""
        async with self._session.begin():
            await self._lock(user_id, film_id)
            old_rating = await self._session.scalar(
                _UPSERT_LIKE, {'user_id': user_id, 'film_id': film_id, 'rating': rating}
            )
            delta = rating_deltas(old_rating, rating)
            if delta.is_noop:
                return await self._read(film_id)
            return await self._apply(_APPLY_RATING_DELTA, film_id, delta)

    async def delete_rating(self, user_id: uuid.UUID, film_id: uuid.UUID) -> FilmRatingResponse | None:
        """Снимает оценку. ``None`` — оценки не было, менять нечего."""
        async with self._session.begin():
            await self._lock(user_id, film_id)
            old_rating = await self._session.scalar(_DELETE_LIKE, {'user_id': user_id, 'film_id': film_id})
            if old_rating is None:
                return None
            return await self._apply(_SUBTRACT_RATING_DELTA, film_id, rating_deltas(old_rating, None))

    async def get_film_rating(self, film_id: uuid.UUID) -> FilmRatingResponse:
        """Сценарий R3c: чтение преагрегата по первичному ключу.

        Агрегация на лету (R2/R3) наружу не выставлена намеренно: на замерах она
        в 13 раз медленнее и является единственным местом, где 200 мс реально
        могли не выдержать. Фильм без оценок отдаётся нулями, а не 404: сервис не
        владеет каталогом и не может отличить «фильма нет» от «его ещё никто не
        оценивал».
        """
        return await self._read(film_id)

    async def list_liked_films(self, user_id: uuid.UUID, min_rating: int, limit: int, offset: int) -> list[LikedFilm]:
        """Сценарий R1: понравившиеся фильмы пользователя."""
        rows = await self._session.execute(
            _SELECT_USER_LIKES,
            {'user_id': user_id, 'min_rating': min_rating, 'limit': limit, 'offset': offset},
        )
        return [LikedFilm(film_id=row.film_id, rating=row.rating, updated_at=row.updated_at) for row in rows]

    # --- внутреннее ---------------------------------------------------------
    async def _lock(self, user_id: uuid.UUID, film_id: uuid.UUID) -> None:
        await self._session.execute(_ADVISORY_LOCK, {'key': f'{user_id}:{film_id}'})

    async def _read(self, film_id: uuid.UUID) -> FilmRatingResponse:
        row = (await self._session.execute(_SELECT_FILM_RATING, {'film_id': film_id})).first()
        if row is None:
            return build_film_rating(film_id, 0, 0, list(_EMPTY_HISTOGRAM))
        return build_film_rating(film_id, row.ratings_count, row.ratings_sum, list(row.hist))

    async def _apply(self, statement, film_id: uuid.UUID, delta: RatingDelta) -> FilmRatingResponse:
        row = (
            await self._session.execute(
                statement,
                {
                    'film_id': film_id,
                    'd_count': delta.count,
                    'd_sum': delta.total,
                    'd_hist': delta.histogram,
                    'hist_size': HISTOGRAM_SIZE,
                },
            )
        ).first()
        if row is None:
            # Сюда можно попасть только при снятии оценки у фильма, строки
            # преагрегата которого нет, — то есть при уже случившемся
            # расхождении. Чинится `python -m practix_ugc_api.cli recount`.
            logger.warning('Преагрегат отсутствует при снятии оценки, film_id=%s', film_id)
            return build_film_rating(film_id, 0, 0, list(_EMPTY_HISTOGRAM))
        return build_film_rating(film_id, row.ratings_count, row.ratings_sum, list(row.hist))
