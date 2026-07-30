"""Рецензии и голоса за них.

Порядки сортировки — три готовых запроса, собранных на импорте из белого списка
колонок, а не одна строка с подстановкой на лету. Имя колонки в ``ORDER BY``
параметром не передаётся, и любая конкатенация с внешней строкой была бы
SQL-инъекцией; словарь готовых запросов делает её структурно невозможной.

Голоса живут по тем же правилам, что и оценки (подробный разбор — в
``rating_service``): счётчики в рецензии денормализованы, состязания на паре
(рецензия, голосующий) те же два, и закрываются они той же
``pg_advisory_xact_lock``, а не блокировкой строки рецензии — иначе голосующие за
одну популярную рецензию выстроились бы в очередь.

Внешних ключей в схеме нет (их не было и в стенде, где мерились планы), поэтому
роль ``ON DELETE CASCADE`` играет транзакция: голос за несуществующую рецензию
вставляется, обновление счётчиков не находит строки, и откат убирает вставленный
голос.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from practix_ugc_api.models.schemas import (
    ReviewCreateRequest,
    ReviewListItem,
    ReviewResponse,
    ReviewSort,
    ReviewVotesResponse,
)
from practix_ugc_api.services.aggregate import vote_deltas
from practix_ugc_api.services.exceptions import ConflictError, ForbiddenError, NotFoundError

# Колонка сортировки на каждый поддерживаемый порядок. Ровно те три, под которые
# в схеме заведены индексы: новый алгоритм ранжирования стоит одного индекса и
# одной строки здесь, а не переезда схемы.
_ORDER_BY: dict[ReviewSort, str] = {
    ReviewSort.NEW: 'created_at DESC',
    ReviewSort.USEFUL: 'useful_score DESC',
    # NULLS LAST — см. комментарий у индекса reviews_film_author_rating_idx:
    # оценка автора необязательна, а PostgreSQL при DESC ставит NULL первыми.
    ReviewSort.RATING: 'author_rating DESC NULLS LAST',
}

_REVIEW_COLUMNS = 'review_id, film_id, user_id, author_rating, created_at, votes_likes, votes_dislikes, useful_score'

# Сценарий R5. `review_id` вторым ключом сортировки — чтобы порядок был
# устойчивым: без него строки с одинаковой полезностью могут переставляться
# между страницами, и пользователь увидит одну рецензию дважды.
_SELECT_REVIEWS = {
    sort: text(
        f"""
        SELECT {_REVIEW_COLUMNS}
        FROM reviews
        WHERE film_id = :film_id
        ORDER BY {order}, review_id
        LIMIT :limit OFFSET :offset
        """
    )
    for sort, order in _ORDER_BY.items()
}

_INSERT_REVIEW = text(
    f"""
    INSERT INTO reviews (review_id, film_id, user_id, body, author_rating, created_at)
    VALUES (:review_id, :film_id, :user_id, :body, :author_rating, now())
    ON CONFLICT (film_id, user_id) DO NOTHING
    RETURNING {_REVIEW_COLUMNS}, body
    """
)

_SELECT_REVIEW = text(f'SELECT {_REVIEW_COLUMNS}, body FROM reviews WHERE review_id = :review_id')
_SELECT_REVIEW_AUTHOR = text('SELECT user_id FROM reviews WHERE review_id = :review_id')
_SELECT_REVIEW_VOTES = text(
    'SELECT votes_likes, votes_dislikes, useful_score FROM reviews WHERE review_id = :review_id'
)

# Порядок удаления важен: сначала рецензия, потом её голоса. Голосующий, успевший
# захватить строку рецензии, доработает раньше нас, и его голос попадёт под
# последующее удаление; голосующий, пришедший после, не найдёт строки и откатится.
_DELETE_REVIEW = text('DELETE FROM reviews WHERE review_id = :review_id RETURNING review_id')
_DELETE_REVIEW_VOTES = text('DELETE FROM review_votes WHERE review_id = :review_id')

_ADVISORY_LOCK = text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))')

_UPSERT_VOTE = text(
    """
    WITH previous AS (
        SELECT value FROM review_votes WHERE review_id = :review_id AND user_id = :user_id
    ), upserted AS (
        INSERT INTO review_votes (review_id, user_id, value, created_at)
        VALUES (:review_id, :user_id, :value, now())
        ON CONFLICT (review_id, user_id) DO UPDATE SET value = EXCLUDED.value
        RETURNING 1
    )
    SELECT (SELECT value FROM previous) AS old_value
    """
)

_DELETE_VOTE = text('DELETE FROM review_votes WHERE review_id = :review_id AND user_id = :user_id RETURNING value')

_APPLY_VOTE_DELTA = text(
    """
    UPDATE reviews SET
        votes_likes    = votes_likes + :d_likes,
        votes_dislikes = votes_dislikes + :d_dislikes,
        useful_score   = useful_score + :d_useful
    WHERE review_id = :review_id
    RETURNING votes_likes, votes_dislikes, useful_score
    """
)


class ReviewService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, user_id: uuid.UUID, request: ReviewCreateRequest) -> ReviewResponse:
        async with self._session.begin():
            row = (
                await self._session.execute(
                    _INSERT_REVIEW,
                    {
                        'review_id': uuid.uuid4(),
                        'film_id': request.film_id,
                        'user_id': user_id,
                        'body': request.body,
                        'author_rating': request.author_rating,
                    },
                )
            ).first()
        if row is None:
            raise ConflictError('У пользователя уже есть рецензия на этот фильм')
        return ReviewResponse(**row._mapping)

    async def get(self, review_id: uuid.UUID) -> ReviewResponse:
        row = (await self._session.execute(_SELECT_REVIEW, {'review_id': review_id})).first()
        if row is None:
            raise NotFoundError('Рецензия не найдена')
        return ReviewResponse(**row._mapping)

    async def delete(self, review_id: uuid.UUID, user_id: uuid.UUID, is_moderator: bool) -> None:
        async with self._session.begin():
            author = await self._session.scalar(_SELECT_REVIEW_AUTHOR, {'review_id': review_id})
            if author is None:
                raise NotFoundError('Рецензия не найдена')
            if author != user_id and not is_moderator:
                raise ForbiddenError('Удалить можно только свою рецензию')
            deleted = await self._session.scalar(_DELETE_REVIEW, {'review_id': review_id})
            if deleted is None:
                raise NotFoundError('Рецензия не найдена')
            await self._session.execute(_DELETE_REVIEW_VOTES, {'review_id': review_id})

    async def list_for_film(
        self, film_id: uuid.UUID, sort: ReviewSort, limit: int, offset: int
    ) -> list[ReviewListItem]:
        rows = await self._session.execute(
            _SELECT_REVIEWS[sort], {'film_id': film_id, 'limit': limit, 'offset': offset}
        )
        return [ReviewListItem(**row._mapping) for row in rows]

    async def vote(self, review_id: uuid.UUID, user_id: uuid.UUID, value: int) -> ReviewVotesResponse:
        async with self._session.begin():
            await self._lock(review_id, user_id)
            old_value = await self._session.scalar(
                _UPSERT_VOTE, {'review_id': review_id, 'user_id': user_id, 'value': value}
            )
            return await self._apply(review_id, vote_deltas(old_value, value))

    async def retract_vote(self, review_id: uuid.UUID, user_id: uuid.UUID) -> ReviewVotesResponse | None:
        """``None`` — голоса не было, менять нечего."""
        async with self._session.begin():
            await self._lock(review_id, user_id)
            old_value = await self._session.scalar(_DELETE_VOTE, {'review_id': review_id, 'user_id': user_id})
            if old_value is None:
                # Отличаем «нет рецензии» от «нет голоса»: первое — 404 на
                # ресурс, второе — нечего отзывать.
                if await self._session.scalar(_SELECT_REVIEW_AUTHOR, {'review_id': review_id}) is None:
                    raise NotFoundError('Рецензия не найдена')
                return None
            return await self._apply(review_id, vote_deltas(old_value, None))

    # --- внутреннее ---------------------------------------------------------
    async def _lock(self, review_id: uuid.UUID, user_id: uuid.UUID) -> None:
        await self._session.execute(_ADVISORY_LOCK, {'key': f'{review_id}:{user_id}'})

    async def _apply(self, review_id: uuid.UUID, deltas: tuple[int, int, int]) -> ReviewVotesResponse:
        d_likes, d_dislikes, d_useful = deltas
        if not (d_likes or d_dislikes or d_useful):
            # Повторный такой же голос: счётчики не двигаются, но существование
            # рецензии всё равно нужно подтвердить.
            row = (await self._session.execute(_SELECT_REVIEW_VOTES, {'review_id': review_id})).first()
        else:
            row = (
                await self._session.execute(
                    _APPLY_VOTE_DELTA,
                    {'review_id': review_id, 'd_likes': d_likes, 'd_dislikes': d_dislikes, 'd_useful': d_useful},
                )
            ).first()
        if row is None:
            # Рецензии нет. Откат уберёт голос, вставленный несколькими строками
            # выше, — это и есть тот ON DELETE CASCADE, которого нет в схеме.
            raise NotFoundError('Рецензия не найдена')
        return ReviewVotesResponse(
            review_id=review_id,
            votes_likes=row.votes_likes,
            votes_dislikes=row.votes_dislikes,
            useful_score=row.useful_score,
        )
