"""
Детерминированный датасет исследования.

Один и тот же набор данных заливается во все три хранилища, а бенчмарк должен
знать существующие идентификаторы, НЕ спрашивая об этом базу. Иначе сравнение
нечестно дважды: подготовительный запрос сам попадает в измерение, а разные
хранилища возвращают разные выборки и меряются на разных данных.

Отсюда правило модуля: любая величина выводится из seed и порядкового номера,
и ничего не хранится между запусками.

Два неочевидных решения:

1. Идентификаторы получаются хешированием порядкового номера, а не подстановкой
   его в UUID. Популярность фильма зависит от номера; если бы номер задавал
   старшие биты UUID, популярные фильмы легли бы в один диапазон ключа
   шардирования и создали искусственно горячий шард. Хеш разрывает эту связь —
   ровно так же, как реальные случайные UUID.

2. Тексты рецензий берутся из пула заранее сгенерированных абзацев. 500 тысяч
   вызовов Faker занимают больше времени, чем сама запись в базу, и мерили бы мы
   тогда Faker.
"""

from __future__ import annotations

import hashlib
import random
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

SEED = 42

# Начало отсчёта для created_at/updated_at. Фиксированное: «сегодня» сделало бы
# датасет невоспроизводимым между прогонами.
EPOCH = datetime(2024, 1, 1, tzinfo=UTC)
PERIOD_SECONDS = 2 * 365 * 24 * 3600

# Порог, по которому оценка считается лайком. Продуктовое решение, которое будет
# меняться, — поэтому в преагрегате лежит гистограмма, а не пара счётчиков.
LIKE_THRESHOLD = 6

_NS_USER = 1
_NS_FILM = 2
_NS_REVIEW = 3


def _stable_uuid(namespace: int, idx: int) -> uuid.UUID:
    digest = hashlib.blake2b(f'{namespace}:{idx}'.encode(), digest_size=16).digest()
    return uuid.UUID(bytes=digest)


def user_uuid(idx: int) -> uuid.UUID:
    return _stable_uuid(_NS_USER, idx)


def film_uuid(idx: int) -> uuid.UUID:
    return _stable_uuid(_NS_FILM, idx)


def review_uuid(idx: int) -> uuid.UUID:
    return _stable_uuid(_NS_REVIEW, idx)


@dataclass(frozen=True)
class Profile:
    """Размерность прогона.

    `review_votes` — ожидаемое число голосов, а не жёсткий лимит: фактическое
    определяется счётчиками рецензий, см. `iter_review_votes`.
    """

    name: str
    users: int
    films: int
    likes: int
    bookmarks: int
    reviews: int
    review_votes: int


# Класс объёма S из задания.
FULL = Profile(
    name='full',
    users=1_000_000,
    films=10_000,
    likes=10_000_000,
    bookmarks=1_000_000,
    reviews=500_000,
    review_votes=2_000_000,
)

# 1/100 объёма — чтобы убедиться, что стенд и скрипты работают, ДО долгого
# прогона. Число фильмов уменьшено тоже, иначе плотность лайков на фильм
# отличалась бы от боевой на два порядка и smoke ничего бы не проверял.
SMOKE = Profile(
    name='smoke',
    users=20_000,
    films=1_000,
    likes=100_000,
    bookmarks=10_000,
    reviews=5_000,
    review_votes=20_000,
)

PROFILES = {FULL.name: FULL, SMOKE.name: SMOKE}


class Dataset:
    """Генератор строк и источник идентификаторов для бенчмарка."""

    def __init__(self, profile: Profile, seed: int = SEED) -> None:
        self.profile = profile
        self.seed = seed

    # --- случайность, привязанная к сущности ------------------------------
    def _rng(self, kind: int, idx: int) -> random.Random:
        return random.Random(self.seed * 1_000_003 + kind * 7_919 + idx)

    # --- распределения -----------------------------------------------------
    def likes_per_user(self, user_idx: int) -> int:
        """Сколько оценок поставил пользователь.

        Распределение намеренно неравномерное: если раздать всем поровну, у
        каждого выйдет по 10 фильмов, и сценарий «список понравившегося»
        выродится в тривиальный. Настоящая аудитория — это молчаливое
        большинство и небольшая доля активных зрителей.
        """
        rng = self._rng(1, user_idx)
        p = rng.random()
        if p < 0.80:
            return rng.randint(1, 20)
        if p < 0.95:
            return rng.randint(20, 100)
        return rng.randint(100, 400)

    def bookmarks_per_user(self, user_idx: int) -> int:
        return self._rng(2, user_idx).randint(1, 30)

    def _film_idx(self, rng: random.Random) -> int:
        """Популярность фильмов — степенная: несколько хитов и длинный хвост.

        Именно хиты дают тяжёлый случай для «посчитать лайки фильма»: у самого
        популярного набирается несколько сотен тысяч оценок. Равномерное
        распределение спрятало бы эту проблему.
        """
        return min(self.profile.films - 1, int(self.profile.films * rng.random() ** 3))

    def _moment(self, rng: random.Random) -> datetime:
        return EPOCH + timedelta(seconds=rng.randrange(PERIOD_SECONDS))

    # --- идентификаторы для бенчмарка -------------------------------------
    def covered_users(self, kind: str = 'likes') -> int:
        """Сколько пользователей успел покрыть генератор до исчерпания лимита.

        Проигрывается тот же цикл, что и в генерации, но без обращения к
        фильмам — миллион дешёвых итераций вместо десяти миллионов строк.
        """
        total = self.profile.likes if kind == 'likes' else self.profile.bookmarks
        per_user = self.likes_per_user if kind == 'likes' else self.bookmarks_per_user
        emitted = 0
        for user_idx in range(self.profile.users):
            emitted += per_user(user_idx)
            if emitted >= total:
                return user_idx + 1
        return self.profile.users

    def active_users(self, minimum: int = 50, limit: int = 5_000) -> list[int]:
        """Пользователи хотя бы с `minimum` оценок.

        Бенчмарк читает списки именно у них: у пользователя с двумя лайками
        любой запрос быстр в любом хранилище, и такое измерение ничего не
        различает.
        """
        covered = self.covered_users('likes')
        found: list[int] = []
        for user_idx in range(covered):
            if self.likes_per_user(user_idx) >= minimum:
                found.append(user_idx)
                if len(found) >= limit:
                    break
        return found

    def sample_films(self, count: int, seed_offset: int = 0) -> list[int]:
        """Фильмы под тем же законом популярности, что и в данных.

        Так в выборку попадают в основном хиты — то есть худший случай для
        агрегации, а не средний по каталогу.
        """
        rng = random.Random(self.seed * 31 + 7 + seed_offset)
        return [self._film_idx(rng) for _ in range(count)]

    # --- генераторы строк --------------------------------------------------
    def iter_likes(self) -> Iterator[dict]:
        emitted = 0
        for user_idx in range(self.profile.users):
            if emitted >= self.profile.likes:
                return
            rng = self._rng(11, user_idx)
            wanted = min(self.likes_per_user(user_idx), self.profile.likes - emitted)
            seen: set[int] = set()
            user = user_uuid(user_idx)
            while len(seen) < wanted:
                film_idx = self._film_idx(rng)
                if film_idx in seen:
                    # Хиты выбираются часто, столкновения неизбежны. Линейный
                    # сдвиг дешевле повторного розыгрыша и не ломает картину
                    # популярности: соседние номера столь же случайны.
                    film_idx = (film_idx + 1) % self.profile.films
                    if film_idx in seen:
                        continue
                seen.add(film_idx)
                created = self._moment(rng)
                yield {
                    'user_id': user,
                    'film_id': film_uuid(film_idx),
                    'rating': rng.randint(0, 10),
                    'created_at': created,
                    'updated_at': created,
                }
                emitted += 1

    def iter_bookmarks(self) -> Iterator[dict]:
        emitted = 0
        for user_idx in range(self.profile.users):
            if emitted >= self.profile.bookmarks:
                return
            rng = self._rng(12, user_idx)
            wanted = min(self.bookmarks_per_user(user_idx), self.profile.bookmarks - emitted)
            seen: set[int] = set()
            user = user_uuid(user_idx)
            while len(seen) < wanted:
                film_idx = self._film_idx(rng)
                if film_idx in seen:
                    film_idx = (film_idx + 1) % self.profile.films
                    if film_idx in seen:
                        continue
                seen.add(film_idx)
                yield {
                    'user_id': user,
                    'film_id': film_uuid(film_idx),
                    'created_at': self._moment(rng),
                }
                emitted += 1

    def votes_for_review(self, review_idx: int) -> tuple[int, int]:
        """Голоса за рецензию: (лайки, дизлайки).

        Считается по тому же seed, что и сами голоса в `iter_review_votes`,
        поэтому денормализованные счётчики в рецензии всегда сходятся с
        таблицей голосов. Расхождение счётчика и источника — классическая
        ошибка денормализации, и в стенде для неё не должно быть места.
        """
        rng = self._rng(13, review_idx)
        # Множитель подобран так, чтобы среднее (scale / 3 при квадрате
        # равномерной величины) дало review_votes / reviews из профиля.
        total = int(rng.random() ** 2 * 12)
        likes = sum(1 for _ in range(total) if rng.random() < 0.7)
        return likes, total - likes

    def iter_reviews(self, body_pool: list[str]) -> Iterator[dict]:
        for review_idx in range(self.profile.reviews):
            rng = self._rng(14, review_idx)
            likes, dislikes = self.votes_for_review(review_idx)
            yield {
                'review_id': review_uuid(review_idx),
                'film_id': film_uuid(self._film_idx(rng)),
                'user_id': user_uuid(rng.randrange(self.profile.users)),
                'body': body_pool[rng.randrange(len(body_pool))],
                'author_rating': rng.randint(0, 10),
                'created_at': self._moment(rng),
                'votes_likes': likes,
                'votes_dislikes': dislikes,
                'useful_score': likes - dislikes,
            }

    def iter_review_votes(self) -> Iterator[dict]:
        """Голоса ровно в том количестве, которое записано в счётчиках рецензии.

        Отсюда два следствия, важные для честности стенда. Во-первых, никакого
        общего лимита: `Profile.review_votes` — ожидаемая величина для прогресса,
        а не потолок; обрезав поток на круглом числе, мы получили бы рецензии,
        у которых счётчик не сходится с таблицей голосов. Во-вторых, при
        повторном выпадении того же голосующего строка не пропускается, а
        переразыгрывается — по той же причине.
        """
        for review_idx in range(self.profile.reviews):
            likes, dislikes = self.votes_for_review(review_idx)
            rng = self._rng(15, review_idx)
            review = review_uuid(review_idx)
            voters: set[int] = set()
            for position in range(likes + dislikes):
                voter = rng.randrange(self.profile.users)
                while voter in voters:
                    voter = rng.randrange(self.profile.users)
                voters.add(voter)
                yield {
                    'review_id': review,
                    'user_id': user_uuid(voter),
                    'value': 1 if position < likes else -1,
                    'created_at': self._moment(rng),
                }


def build_body_pool(size: int = 1_000, seed: int = SEED) -> list[str]:
    """Пул текстов рецензий.

    Faker — если он установлен; иначе детерминированная заглушка, чтобы стенд
    поднимался и без него (генератор данных не должен падать из-за библиотеки,
    которая рисует текст).
    """
    try:
        from faker import Faker
    except ImportError:
        rng = random.Random(seed)
        words = [f'слово{i}' for i in range(200)]
        return [' '.join(rng.choices(words, k=rng.randint(40, 120))) for _ in range(size)]

    faker = Faker('ru_RU')
    Faker.seed(seed)
    return [faker.paragraph(nb_sentences=8) for _ in range(size)]
