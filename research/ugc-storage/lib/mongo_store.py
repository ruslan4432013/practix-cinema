"""
Адаптер MongoDB — основной кандидат исследования.

Два решения, которые видно в коде и которые обсуждаются в README:

1. Идентификаторы хранятся строками, а не BinData(UUID). Строка на 20 байт
   дороже, зато границы чанков читаемы в `sh.status()`, а пресплит по первому
   hex-символу тривиален. На объёме S разница в объёме несущественна, разница в
   управляемости — нет.

2. Гистограмма оценок в преагрегате — поддокумент с ключами '0'..'10', а не
   массив. `$inc` умеет и то и другое, но при upsert'е несуществующего документа
   путь `hist.7` создаёт объект, а не массив; выбирать между двумя формами
   представления в зависимости от того, первый это голос за фильм или нет, —
   верный способ получить редкий и неуловимый баг.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

from pymongo import MongoClient, ReturnDocument, UpdateOne
from pymongo.errors import BulkWriteError

from lib.dataset import LIKE_THRESHOLD
from lib.store import REVIEW_ORDERS, rating_deltas

COLLECTIONS = ('likes', 'bookmarks', 'reviews', 'review_votes', 'film_rating')


def _sid(value: uuid.UUID) -> str:
    return str(value)


class MongoStore:
    name = 'mongo'

    def __init__(self, host: str | None = None, port: int | None = None, database: str | None = None) -> None:
        host = host or os.getenv('MONGO_HOST', 'localhost')
        port = port or int(os.getenv('MONGO_PORT', '27017'))
        self.client: MongoClient = MongoClient(host, port, uuidRepresentation='standard', tz_aware=True)
        self.db = self.client[database or os.getenv('MONGO_DB', 'ugc')]

    # --- запись ------------------------------------------------------------
    def _insert(self, collection: str, documents: list[dict]) -> None:
        """Вставка пачки с внятным сообщением об ошибке.

        BulkWriteError печатает подробности по КАЖДОЙ из десяти тысяч
        отвергнутых операций — это мегабайты вывода, в которых сама причина
        теряется. Здесь остаются счётчик и первое сообщение: когда отваливается
        шард, все ошибки в пачке всё равно одинаковые.
        """
        try:
            self.db[collection].insert_many(documents, ordered=False)
        except BulkWriteError as error:
            errors = error.details.get('writeErrors', [])
            first = errors[0]['errmsg'] if errors else 'подробности недоступны'
            raise SystemExit(
                f'{collection}: отвергнуто {len(errors)} из {len(documents)} документов. Первая ошибка: {first}'
            ) from None

    def write_likes(self, rows: list[dict]) -> None:
        self._insert(
            'likes',
            [
                {
                    'film_id': _sid(r['film_id']),
                    'user_id': _sid(r['user_id']),
                    'rating': int(r['rating']),
                    'created_at': r['created_at'],
                    'updated_at': r['updated_at'],
                }
                for r in rows
            ],
        )

    def write_bookmarks(self, rows: list[dict]) -> None:
        self._insert(
            'bookmarks',
            [
                {'user_id': _sid(r['user_id']), 'film_id': _sid(r['film_id']), 'created_at': r['created_at']}
                for r in rows
            ],
        )

    def write_reviews(self, rows: list[dict]) -> None:
        self._insert(
            'reviews',
            [
                {
                    '_id': _sid(r['review_id']),
                    'film_id': _sid(r['film_id']),
                    'user_id': _sid(r['user_id']),
                    'body': r['body'],
                    'author_rating': int(r['author_rating']),
                    'created_at': r['created_at'],
                    'votes': {'likes': int(r['votes_likes']), 'dislikes': int(r['votes_dislikes'])},
                    'useful_score': int(r['useful_score']),
                }
                for r in rows
            ],
        )

    def write_review_votes(self, rows: list[dict]) -> None:
        self._insert(
            'review_votes',
            [
                {
                    'review_id': _sid(r['review_id']),
                    'user_id': _sid(r['user_id']),
                    'value': int(r['value']),
                    'created_at': r['created_at'],
                }
                for r in rows
            ],
        )

    def write_film_rating(self, rows: list[dict]) -> None:
        self.db.film_rating.bulk_write(
            [
                UpdateOne(
                    {'_id': _sid(r['film_id'])},
                    {
                        '$set': {
                            'ratings_count': int(r['ratings_count']),
                            'ratings_sum': int(r['ratings_sum']),
                            'hist': {str(i): int(n) for i, n in enumerate(r['hist'])},
                        }
                    },
                    upsert=True,
                )
                for r in rows
            ],
        )

    # --- обслуживание ------------------------------------------------------
    def truncate(self) -> None:
        # delete_many, а не drop: drop снёс бы шардирование, индексы и
        # валидаторы, которые ставил mongo-init, и стенд пришлось бы поднимать
        # заново.
        for name in COLLECTIONS:
            self.db[name].delete_many({})

    def counts(self) -> dict[str, int]:
        return {name: self.db[name].estimated_document_count() for name in COLLECTIONS}

    def optimize(self) -> None:
        return None

    def close(self) -> None:
        self.client.close()

    # --- сценарии чтения ---------------------------------------------------
    def r1_user_liked_films(self, user_id: uuid.UUID) -> list:
        # Запрос без film_id, то есть без ключа шардирования: mongos рассылает
        # его на все шарды и сливает ответы. Цена этого решения и меряется.
        cursor = (
            self.db.likes.find({'user_id': _sid(user_id), 'rating': {'$gte': 8}}, {'film_id': 1, 'rating': 1, '_id': 0})
            .sort([('rating', -1), ('updated_at', -1)])
            .limit(50)
        )
        return list(cursor)

    def r2_film_like_counts(self, film_id: uuid.UUID) -> tuple[int, int]:
        pipeline = [
            {'$match': {'film_id': _sid(film_id)}},
            {
                '$group': {
                    '_id': None,
                    'likes': {'$sum': {'$cond': [{'$gte': ['$rating', LIKE_THRESHOLD]}, 1, 0]}},
                    'dislikes': {'$sum': {'$cond': [{'$lt': ['$rating', LIKE_THRESHOLD]}, 1, 0]}},
                }
            },
        ]
        result = list(self.db.likes.aggregate(pipeline))
        return (result[0]['likes'], result[0]['dislikes']) if result else (0, 0)

    def r3_film_avg_rating(self, film_id: uuid.UUID) -> tuple[float, int]:
        pipeline = [
            {'$match': {'film_id': _sid(film_id)}},
            {'$group': {'_id': None, 'avg': {'$avg': '$rating'}, 'count': {'$sum': 1}}},
        ]
        result = list(self.db.likes.aggregate(pipeline))
        return (float(result[0]['avg']), result[0]['count']) if result else (0.0, 0)

    def r3c_film_avg_cached(self, film_id: uuid.UUID) -> tuple[float, int]:
        doc = self.db.film_rating.find_one({'_id': _sid(film_id)})
        if not doc or not doc.get('ratings_count'):
            return 0.0, 0
        return doc['ratings_sum'] / doc['ratings_count'], doc['ratings_count']

    def r4_user_bookmarks(self, user_id: uuid.UUID) -> list:
        cursor = (
            self.db.bookmarks.find({'user_id': _sid(user_id)}, {'film_id': 1, 'created_at': 1, '_id': 0})
            .sort('created_at', -1)
            .limit(100)
        )
        return list(cursor)

    def r5_film_reviews(self, film_id: uuid.UUID, order: str) -> list:
        field = REVIEW_ORDERS[order]
        cursor = self.db.reviews.find({'film_id': _sid(film_id)}, {'body': 0}).sort([(field, -1), ('_id', 1)]).limit(20)
        return list(cursor)

    # --- сценарий записи ---------------------------------------------------
    def w1_set_rating(self, user_id: uuid.UUID, film_id: uuid.UUID, rating: int) -> None:
        """Два обращения: сама оценка и преагрегат.

        Транзакции в шардированном кластере тут сознательно не используются:
        распределённая транзакция стоит дороже самой записи, а расхождение
        счётчика лечится периодическим пересчётом. Задание требует, чтобы
        изменение было видно быстро, а не чтобы счётчик был точен посекундно.
        """
        now = datetime.now(UTC)
        previous = self.db.likes.find_one_and_update(
            {'film_id': _sid(film_id), 'user_id': _sid(user_id)},
            {'$set': {'rating': int(rating), 'updated_at': now}, '$setOnInsert': {'created_at': now}},
            upsert=True,
            projection={'rating': 1},
            return_document=ReturnDocument.BEFORE,
        )
        d_count, d_sum, d_hist = rating_deltas(previous['rating'] if previous else None, int(rating))
        if d_count == 0 and d_sum == 0 and not d_hist:
            return
        inc = {'ratings_count': d_count, 'ratings_sum': d_sum}
        inc.update({f'hist.{position}': delta for position, delta in d_hist.items()})
        self.db.film_rating.update_one({'_id': _sid(film_id)}, {'$inc': inc}, upsert=True)
