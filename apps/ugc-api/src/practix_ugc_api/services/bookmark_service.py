"""Закладки «посмотреть позже».

Самая простая из трёх сущностей: ни преагрегата, ни счётчиков, ни конкуренции за
общую строку. Поэтому и транзакция здесь одна на один запрос без блокировок.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from practix_ugc_api.models.schemas import BookmarkResponse

# `DO UPDATE SET film_id = bookmarks.film_id` — присвоение самому себе. Нужно
# ради RETURNING: `DO NOTHING` при повторной закладке не вернул бы ни строки, и
# пришлось бы делать второй запрос ради даты создания. Повторная закладка
# идемпотентна: created_at сохраняется, а не сдвигается на «сейчас».
_UPSERT_BOOKMARK = text(
    """
    INSERT INTO bookmarks (user_id, film_id, created_at) VALUES (:user_id, :film_id, now())
    ON CONFLICT (user_id, film_id) DO UPDATE SET film_id = bookmarks.film_id
    RETURNING created_at
    """
)

_DELETE_BOOKMARK = text('DELETE FROM bookmarks WHERE user_id = :user_id AND film_id = :film_id RETURNING film_id')

# Сценарий R4. Индекс bookmarks_user_created_idx построен ровно под него.
_SELECT_BOOKMARKS = text(
    """
    SELECT film_id, created_at
    FROM bookmarks
    WHERE user_id = :user_id
    ORDER BY created_at DESC
    LIMIT :limit OFFSET :offset
    """
)


class BookmarkService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, user_id: uuid.UUID, film_id: uuid.UUID) -> BookmarkResponse:
        async with self._session.begin():
            created_at = await self._session.scalar(_UPSERT_BOOKMARK, {'user_id': user_id, 'film_id': film_id})
        return BookmarkResponse(film_id=film_id, created_at=created_at)

    async def remove(self, user_id: uuid.UUID, film_id: uuid.UUID) -> bool:
        """``False`` — закладки не было."""
        async with self._session.begin():
            removed = await self._session.scalar(_DELETE_BOOKMARK, {'user_id': user_id, 'film_id': film_id})
        return removed is not None

    async def list_for_user(self, user_id: uuid.UUID, limit: int, offset: int) -> list[BookmarkResponse]:
        rows = await self._session.execute(_SELECT_BOOKMARKS, {'user_id': user_id, 'limit': limit, 'offset': offset})
        return [BookmarkResponse(film_id=row.film_id, created_at=row.created_at) for row in rows]
