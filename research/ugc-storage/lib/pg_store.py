"""
Адаптер PostgreSQL — база сравнения.

PostgreSQL в проекте уже есть (theatre-db, auth-db), и прежде чем заводить новое
хранилище, нужно показать, что имеющегося не хватает. Если он проходит по SLA —
самый дешёвый вариант архитектуры оказывается и правильным.

Соединение — потокоlocal: psycopg2-соединение не потокобезопасно, а бенчмарк
умеет гонять сценарии в несколько потоков.
"""

from __future__ import annotations

import os
import threading
import uuid

import psycopg2
import psycopg2.extras

from lib.dataset import LIKE_THRESHOLD
from lib.store import REVIEW_ORDERS, rating_deltas

# Без этого psycopg2 не знает, во что превращать uuid.UUID, и падает на
# «can't adapt type 'UUID'». Регистрация глобальная и делается один раз.
psycopg2.extras.register_uuid()

TABLES = ('likes', 'bookmarks', 'reviews', 'review_votes', 'film_rating')

_R1 = """
    SELECT film_id, rating
    FROM likes
    WHERE user_id = %(user_id)s AND rating >= 8
    ORDER BY rating DESC, updated_at DESC
    LIMIT 50
"""

_R2 = f"""
    SELECT count(*) FILTER (WHERE rating >= {LIKE_THRESHOLD}) AS likes,
           count(*) FILTER (WHERE rating < {LIKE_THRESHOLD}) AS dislikes
    FROM likes
    WHERE film_id = %(film_id)s
"""

_R3 = 'SELECT avg(rating)::float8, count(*) FROM likes WHERE film_id = %(film_id)s'

_R3C = 'SELECT ratings_sum, ratings_count FROM film_rating WHERE film_id = %(film_id)s'

_R4 = """
    SELECT film_id, created_at
    FROM bookmarks
    WHERE user_id = %(user_id)s
    ORDER BY created_at DESC
    LIMIT 100
"""

# Сортировка подставляется из белого списка REVIEW_ORDERS, не из запроса
# пользователя: имя колонки параметром не передашь, а конкатенация с внешней
# строкой — это SQL-инъекция.
_R5 = """
    SELECT review_id, user_id, author_rating, created_at, votes_likes, votes_dislikes, useful_score
    FROM reviews
    WHERE film_id = %(film_id)s
    ORDER BY {order_column} DESC, review_id
    LIMIT 20
"""

# Старое значение оценки нужно, чтобы поправить преагрегат, а ON CONFLICT
# возвращает только новую строку. CTE решает это за один round trip.
_W1_LIKE = """
    WITH previous AS (
        SELECT rating FROM likes WHERE user_id = %(user_id)s AND film_id = %(film_id)s
    ), upserted AS (
        INSERT INTO likes (user_id, film_id, rating, created_at, updated_at)
        VALUES (%(user_id)s, %(film_id)s, %(rating)s, now(), now())
        ON CONFLICT (user_id, film_id)
        DO UPDATE SET rating = EXCLUDED.rating, updated_at = now()
        RETURNING 1
    )
    SELECT (SELECT rating FROM previous) AS old_rating, (SELECT count(*) FROM upserted) AS written
"""

# Гистограмма складывается поэлементно через generate_series, а не через
# многоаргументный unnest: индексация по позиции читается однозначно и не
# зависит от того, в каком порядке unnest отдаст строки.
_W1_RATING = """
    INSERT INTO film_rating (film_id, ratings_count, ratings_sum, hist)
    VALUES (%(film_id)s, %(d_count)s, %(d_sum)s, %(d_hist)s::int[])
    ON CONFLICT (film_id) DO UPDATE SET
        ratings_count = film_rating.ratings_count + %(d_count)s,
        ratings_sum   = film_rating.ratings_sum + %(d_sum)s,
        hist          = (
            SELECT array_agg(film_rating.hist[i] + (%(d_hist)s::int[])[i] ORDER BY i)
            FROM generate_series(1, 11) AS i
        )
"""


class PgStore:
    name = 'postgres'

    def __init__(self, **_: object) -> None:
        self._dsn = {
            'dbname': os.getenv('RESEARCH_PG_DB', 'ugc'),
            'user': os.getenv('RESEARCH_PG_USER', 'research'),
            'password': os.getenv('RESEARCH_PG_PASSWORD', 'research'),
            'host': os.getenv('RESEARCH_PG_HOST', 'localhost'),
            'port': int(os.getenv('RESEARCH_PG_PORT', '5434')),
        }
        self._local = threading.local()
        self._all: list = []
        self._lock = threading.Lock()

    @property
    def _conn(self):
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            conn = psycopg2.connect(**self._dsn)
            conn.autocommit = True
            self._local.conn = conn
            with self._lock:
                self._all.append(conn)
        return conn

    def _fetch(self, sql: str, params: dict) -> list[tuple]:
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    # --- запись ------------------------------------------------------------
    def _copy(self, table: str, columns: tuple[str, ...], values: list[tuple]) -> None:
        with self._conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                f'INSERT INTO {table} ({", ".join(columns)}) VALUES %s ON CONFLICT DO NOTHING',
                values,
                page_size=len(values),
            )

    def write_likes(self, rows: list[dict]) -> None:
        self._copy(
            'likes',
            ('user_id', 'film_id', 'rating', 'created_at', 'updated_at'),
            [(r['user_id'], r['film_id'], r['rating'], r['created_at'], r['updated_at']) for r in rows],
        )

    def write_bookmarks(self, rows: list[dict]) -> None:
        self._copy(
            'bookmarks',
            ('user_id', 'film_id', 'created_at'),
            [(r['user_id'], r['film_id'], r['created_at']) for r in rows],
        )

    def write_reviews(self, rows: list[dict]) -> None:
        self._copy(
            'reviews',
            (
                'review_id',
                'film_id',
                'user_id',
                'body',
                'author_rating',
                'created_at',
                'votes_likes',
                'votes_dislikes',
                'useful_score',
            ),
            [
                (
                    r['review_id'],
                    r['film_id'],
                    r['user_id'],
                    r['body'],
                    r['author_rating'],
                    r['created_at'],
                    r['votes_likes'],
                    r['votes_dislikes'],
                    r['useful_score'],
                )
                for r in rows
            ],
        )

    def write_review_votes(self, rows: list[dict]) -> None:
        self._copy(
            'review_votes',
            ('review_id', 'user_id', 'value', 'created_at'),
            [(r['review_id'], r['user_id'], r['value'], r['created_at']) for r in rows],
        )

    def write_film_rating(self, rows: list[dict]) -> None:
        self._copy(
            'film_rating',
            ('film_id', 'ratings_count', 'ratings_sum', 'hist'),
            [(r['film_id'], r['ratings_count'], r['ratings_sum'], list(r['hist'])) for r in rows],
        )

    # --- обслуживание ------------------------------------------------------
    def truncate(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute(f'TRUNCATE {", ".join(TABLES)}')

    def counts(self) -> dict[str, int]:
        return {table: self._fetch(f'SELECT count(*) FROM {table}', {})[0][0] for table in TABLES}

    def optimize(self) -> None:
        # Без ANALYZE планировщик работает по статистике пустых таблиц и
        # выбирает seq scan там, где есть индекс. Это исказило бы замер сильнее
        # всего остального вместе взятого.
        with self._conn.cursor() as cur:
            cur.execute('VACUUM ANALYZE')

    def close(self) -> None:
        with self._lock:
            for conn in self._all:
                conn.close()
            self._all.clear()

    # --- сценарии чтения ---------------------------------------------------
    def r1_user_liked_films(self, user_id: uuid.UUID) -> list:
        return self._fetch(_R1, {'user_id': str(user_id)})

    def r2_film_like_counts(self, film_id: uuid.UUID) -> tuple[int, int]:
        row = self._fetch(_R2, {'film_id': str(film_id)})[0]
        return int(row[0]), int(row[1])

    def r3_film_avg_rating(self, film_id: uuid.UUID) -> tuple[float, int]:
        row = self._fetch(_R3, {'film_id': str(film_id)})[0]
        return float(row[0] or 0.0), int(row[1])

    def r3c_film_avg_cached(self, film_id: uuid.UUID) -> tuple[float, int]:
        rows = self._fetch(_R3C, {'film_id': str(film_id)})
        if not rows or not rows[0][1]:
            return 0.0, 0
        return rows[0][0] / rows[0][1], rows[0][1]

    def r4_user_bookmarks(self, user_id: uuid.UUID) -> list:
        return self._fetch(_R4, {'user_id': str(user_id)})

    def r5_film_reviews(self, film_id: uuid.UUID, order: str) -> list:
        return self._fetch(_R5.format(order_column=REVIEW_ORDERS[order]), {'film_id': str(film_id)})

    # --- сценарий записи ---------------------------------------------------
    def w1_set_rating(self, user_id: uuid.UUID, film_id: uuid.UUID, rating: int) -> None:
        old_rating = self._fetch(_W1_LIKE, {'user_id': str(user_id), 'film_id': str(film_id), 'rating': rating})[0][0]
        d_count, d_sum, d_hist = rating_deltas(old_rating, rating)
        if d_count == 0 and d_sum == 0 and not d_hist:
            return
        hist_delta = [0] * 11
        for position, delta in d_hist.items():
            hist_delta[position] = delta
        with self._conn.cursor() as cur:
            cur.execute(
                _W1_RATING,
                {'film_id': str(film_id), 'd_count': d_count, 'd_sum': d_sum, 'd_hist': hist_delta},
            )
