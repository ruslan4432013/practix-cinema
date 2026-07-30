"""
Адаптер ClickHouse — вторая база сравнения.

ClickHouse в проекте уже стоит и обслуживает аналитику (apps/etl-clickhouse),
поэтому гипотезу «возьмём его же и под UGC» нужно проверить, а не отмахнуться.
Ожидание — что агрегаты по фильму он посчитает быстрее всех, а на точечных
чтениях и изменениях проиграет: это следствие LSM-подхода, а не настройки.

Клиент — потокоlocal: clickhouse_connect.Client не потокобезопасен.
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import UTC, datetime

import clickhouse_connect

from lib.dataset import LIKE_THRESHOLD
from lib.store import REVIEW_ORDERS, rating_deltas

TABLES = ('likes', 'likes_by_user', 'bookmarks', 'reviews', 'review_votes', 'film_rating')

# Версии схлопываются при слиянии, а когда оно произойдёт — не определено.
# Поэтому любое честное чтение доагрегирует версии само: argMax по updated_at.
_DEDUPED_BY_FILM = """
    SELECT user_id, argMax(rating, updated_at) AS rating
    FROM ugc.likes
    WHERE film_id = {film_id:UUID}
    GROUP BY user_id
"""

_R1 = """
    SELECT film_id, argMax(rating, updated_at) AS rating
    FROM ugc.likes_by_user
    WHERE user_id = {user_id:UUID}
    GROUP BY film_id
    HAVING rating >= 8
    ORDER BY rating DESC
    LIMIT 50
"""

_R2 = f"""
    SELECT countIf(rating >= {LIKE_THRESHOLD}) AS likes,
           countIf(rating < {LIKE_THRESHOLD}) AS dislikes
    FROM ({_DEDUPED_BY_FILM})
"""

_R3 = f'SELECT avg(rating), count() FROM ({_DEDUPED_BY_FILM})'

# sum поверх SummingMergeTree обязателен: до слияния частей строк на фильм
# несколько, и чтение «как есть» вернуло бы только одну из них.
_R3C = """
    SELECT sum(ratings_sum), sum(ratings_count)
    FROM ugc.film_rating
    WHERE film_id = {film_id:UUID}
"""

_R4 = """
    SELECT film_id, max(created_at) AS created_at
    FROM ugc.bookmarks
    WHERE user_id = {user_id:UUID}
    GROUP BY film_id
    ORDER BY created_at DESC
    LIMIT 100
"""

# Рецензии в сценариях исследования не изменяются, поэтому доагрегация версий
# здесь не нужна; при появлении редактирования сюда пришлось бы добавить FINAL
# или argMax — и цена сортировки выросла бы.
#
# Порядок подставляется через replace, а НЕ через str.format: плейсхолдеры
# параметров ClickHouse сами записываются фигурными скобками ({film_id:UUID}), и
# format принимает их за свои поля. Запрос при этом не падает на этапе сборки —
# он ломается уже на сервере, что куда неприятнее.
_R5 = """
    SELECT review_id, user_id, author_rating, created_at, votes_likes, votes_dislikes, useful_score
    FROM ugc.reviews
    WHERE film_id = {film_id:UUID}
    ORDER BY __ORDER__ DESC, review_id
    LIMIT 20
"""

# count() нужен рядом с argMax: на пустой выборке argMax отдаёт значение по
# умолчанию (0), а 0 — законная оценка. Без счётчика «оценки не было» и
# «оценка была нулевой» неразличимы, и гистограмма поехала бы.
_W1_PREVIOUS = """
    SELECT count() AS versions, argMax(rating, updated_at) AS rating
    FROM ugc.likes
    WHERE film_id = {film_id:UUID} AND user_id = {user_id:UUID}
"""


class ClickHouseStore:
    name = 'clickhouse'

    def __init__(self, **_: object) -> None:
        self._settings = {
            'host': os.getenv('RESEARCH_CH_HOST', 'localhost'),
            'port': int(os.getenv('RESEARCH_CH_PORT', '8127')),
            'username': os.getenv('RESEARCH_CH_USER', 'research'),
            'password': os.getenv('RESEARCH_CH_PASSWORD', 'research'),
            'database': os.getenv('RESEARCH_CH_DB', 'ugc'),
        }
        self._local = threading.local()
        self._all: list = []
        self._lock = threading.Lock()

    @property
    def _client(self):
        client = getattr(self._local, 'client', None)
        if client is None:
            client = clickhouse_connect.get_client(**self._settings)
            self._local.client = client
            with self._lock:
                self._all.append(client)
        return client

    def _rows(self, sql: str, params: dict | None = None) -> list[tuple]:
        return self._client.query(sql, parameters=params or {}).result_rows

    # --- запись ------------------------------------------------------------
    def write_likes(self, rows: list[dict]) -> None:
        self._client.insert(
            'ugc.likes',
            [(r['film_id'], r['user_id'], r['rating'], r['created_at'], r['updated_at']) for r in rows],
            column_names=['film_id', 'user_id', 'rating', 'created_at', 'updated_at'],
        )

    def write_bookmarks(self, rows: list[dict]) -> None:
        self._client.insert(
            'ugc.bookmarks',
            [(r['user_id'], r['film_id'], r['created_at']) for r in rows],
            column_names=['user_id', 'film_id', 'created_at'],
        )

    def write_reviews(self, rows: list[dict]) -> None:
        self._client.insert(
            'ugc.reviews',
            [
                (
                    r['film_id'],
                    r['review_id'],
                    r['user_id'],
                    r['body'],
                    r['author_rating'],
                    r['created_at'],
                    r['votes_likes'],
                    r['votes_dislikes'],
                    r['useful_score'],
                    r['created_at'],
                )
                for r in rows
            ],
            column_names=[
                'film_id',
                'review_id',
                'user_id',
                'body',
                'author_rating',
                'created_at',
                'votes_likes',
                'votes_dislikes',
                'useful_score',
                'updated_at',
            ],
        )

    def write_review_votes(self, rows: list[dict]) -> None:
        self._client.insert(
            'ugc.review_votes',
            [(r['review_id'], r['user_id'], r['value'], r['created_at']) for r in rows],
            column_names=['review_id', 'user_id', 'value', 'created_at'],
        )

    def write_film_rating(self, rows: list[dict]) -> None:
        self._client.insert(
            'ugc.film_rating',
            [(r['film_id'], r['ratings_count'], r['ratings_sum'], list(r['hist'])) for r in rows],
            column_names=['film_id', 'ratings_count', 'ratings_sum', 'hist'],
        )

    # --- обслуживание ------------------------------------------------------
    def truncate(self) -> None:
        for table in TABLES:
            self._client.command(f'TRUNCATE TABLE IF EXISTS ugc.{table}')

    def counts(self) -> dict[str, int]:
        return {table: int(self._rows(f'SELECT count() FROM ugc.{table}')[0][0]) for table in TABLES}

    def optimize(self) -> None:
        # OPTIMIZE FINAL даёт ClickHouse лучший из возможных раскладов: все
        # версии схлопнуты, читать нечего лишнего. Это сознательная фора —
        # аналог VACUUM ANALYZE для PostgreSQL. В бою такого состояния не
        # бывает, и об этом сказано в README.
        for table in TABLES:
            self._client.command(f'OPTIMIZE TABLE ugc.{table} FINAL', settings={'receive_timeout': 1800})

    def close(self) -> None:
        with self._lock:
            for client in self._all:
                client.close()
            self._all.clear()

    # --- сценарии чтения ---------------------------------------------------
    def r1_user_liked_films(self, user_id: uuid.UUID) -> list:
        return self._rows(_R1, {'user_id': user_id})

    def r2_film_like_counts(self, film_id: uuid.UUID) -> tuple[int, int]:
        row = self._rows(_R2, {'film_id': film_id})[0]
        return int(row[0]), int(row[1])

    def r3_film_avg_rating(self, film_id: uuid.UUID) -> tuple[float, int]:
        row = self._rows(_R3, {'film_id': film_id})[0]
        return float(row[0] or 0.0), int(row[1])

    def r3c_film_avg_cached(self, film_id: uuid.UUID) -> tuple[float, int]:
        row = self._rows(_R3C, {'film_id': film_id})[0]
        count = int(row[1] or 0)
        return (float(row[0]) / count if count else 0.0), count

    def r4_user_bookmarks(self, user_id: uuid.UUID) -> list:
        return self._rows(_R4, {'user_id': user_id})

    def r5_film_reviews(self, film_id: uuid.UUID, order: str) -> list:
        return self._rows(_R5.replace('__ORDER__', REVIEW_ORDERS[order]), {'film_id': film_id})

    # --- сценарий записи ---------------------------------------------------
    def w1_set_rating(self, user_id: uuid.UUID, film_id: uuid.UUID, rating: int) -> None:
        """Три обращения против двух у Mongo и PostgreSQL.

        Лишнее — чтение предыдущей оценки: без него не посчитать дельту для
        гистограммы, а получить старое значение «попутно» с записью ClickHouse
        не умеет: у INSERT нет ни RETURNING, ни аналога findOneAndUpdate.
        """
        versions, last_rating = self._rows(_W1_PREVIOUS, {'film_id': film_id, 'user_id': user_id})[0]
        previous = int(last_rating) if versions else None

        now = datetime.now(UTC)
        self._client.insert(
            'ugc.likes',
            [(film_id, user_id, rating, now, now)],
            column_names=['film_id', 'user_id', 'rating', 'created_at', 'updated_at'],
        )

        d_count, d_sum, d_hist = rating_deltas(previous, rating)
        if d_count == 0 and d_sum == 0 and not d_hist:
            return
        hist_delta = [0] * 11
        for position, delta in d_hist.items():
            hist_delta[position] = delta
        self._client.insert(
            'ugc.film_rating',
            [(film_id, d_count, d_sum, hist_delta)],
            column_names=['film_id', 'ratings_count', 'ratings_sum', 'hist'],
        )
