"""Генератор синтетической истории просмотров (E2, T2.3).

ЗАЧЕМ ОН ВООБЩЕ НУЖЕН. Стенд поднимается с пустым ClickHouse: события пишет
только живой трафик, которого на стенде нет. Раздел 8 ТЗ числит «данных мало
для обучения» риском номер один — с высокой вероятностью и блокирующим
влиянием, — и прямо закладывает генератор в DoD эпика E2.

ВКУСОВЫЕ КЛАСТЕРЫ — НЕ УКРАШЕНИЕ, А УСЛОВИЕ ОСМЫСЛЕННОСТИ ВСЕЙ ПРОВЕРКИ. Если
раздавать просмотры равномерно случайно, со-встречаемость по построению
выродится в шум: у любых двух фильмов будет примерно одинаковое пересечение
аудитории. Метрики качества (E5) тогда покажут, что модель не лучше baseline, и
это будет правдой — но правдой о генераторе, а не о модели. Поэтому фильмы
разбиваются на кластеры («жанровые» по смыслу, безымянные по реализации), а
пользователь выбирает в основном из своих.

ПОПУЛЯРНОСТЬ СТЕПЕННАЯ, АКТИВНОСТЬ С ТЯЖЁЛЫМ ХВОСТОМ — тот же подход, что в
``research/ugc-storage/lib/dataset.py``. Равномерное распределение сделало бы
«популярное» бессмысленным (все фильмы одинаковы), а покрытие каталога —
искусственно прекрасным.

ФИЛЬМЫ БЕРУТСЯ НАСТОЯЩИЕ, из ``theatre-db``. Выдуманные UUID дали бы
рекомендации на фильмы, которых нет в каталоге: сквозной путь превратился бы в
проверку самого себя, а ``?enrich=true`` не нашёл бы ни одной карточки.

ВСЁ ДЕТЕРМИНИРОВАНО ПО SEED. Прогон, который нельзя повторить, нельзя и
отладить: «вчера метрики были лучше» без воспроизводимости не диагноз.
"""

import datetime
import hashlib
import random
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

# Столько фильмов приходится на один «вкусовой кластер». Не число кластеров, а
# их плотность: каталог на стенде может быть и 999 фильмов, и 200 000, а размер
# кластера, при котором пересечения аудитории заметны, от этого не зависит.
FILMS_PER_CLUSTER = 40
# Доля просмотров, которую пользователь берёт вне своих кластеров. Ноль дал бы
# идеально разделимые группы, каких не бывает; половина — снова шум.
EXPLORATION_RATE = 0.15
# Профиль активности: (доля пользователей, диапазон числа фильмов).
ACTIVITY_PROFILE = ((0.80, (2, 12)), (0.15, (12, 40)), (0.05, (40, 120)))
# Типичная длительность фильма — полтора-три часа.
DURATION_RANGE_MS = (5_400_000, 10_800_000)
# Словарь качества — ТОТ ЖЕ, что у коллектора (VideoQuality в его models/enums.py).
# Список здесь продублирован, а не импортирован: батч не может зависеть от
# приложения-соседа, а в practix-contracts лежит вокабуляр событий и топиков, но
# не перечисление качества. Расхождение при этом не остаётся незамеченным —
# сливом `--sink api` синтетика проходит валидацию настоящего коллектора, и
# первый же прогон отвечает 422 на выдуманное значение (так и было поймано
# исходное '4k' вместо '2160p').
QUALITIES = ('480p', '720p', '1080p', '2160p')
DEVICE_TYPES = ('desktop', 'mobile', 'tablet', 'tv')


def user_uuid(index: int, seed: int) -> str:
    """Детерминированный UUID пользователя.

    Из хеша, а не из ``uuid4``: при том же seed нужен тот же набор
    пользователей, иначе повторный прогон генератора создал бы вторую
    непересекающуюся аудиторию и удвоил витрину, вместо того чтобы дополнить
    историю существующей.
    """
    digest = hashlib.blake2b(f'recsys-user:{seed}:{index}'.encode(), digest_size=16).digest()
    return str(uuid.UUID(bytes=digest))


@dataclass(frozen=True, slots=True)
class ViewSession:
    """Один сеанс просмотра: кто, что, когда и насколько досмотрел."""

    user_id: str
    film_id: str
    session_id: str
    started_at: datetime.datetime
    duration_ms: int
    completion_rate: float
    quality: str
    device_type: str

    @property
    def view_id(self) -> str:
        """Тот же ключ, что строит ETL: ``session_id:film_id``."""
        return f'{self.session_id}:{self.film_id}'

    @property
    def watched_ms(self) -> int:
        return int(self.duration_ms * self.completion_rate)

    @property
    def completed(self) -> bool:
        """Досмотром считается 90% — тот же порог, что у витрины досматриваемости."""
        return self.completion_rate >= 0.9


class Generator:
    """Детерминированный генератор сеансов просмотра."""

    def __init__(self, film_ids: list[str], *, seed: int, users: int, days: int) -> None:
        if not film_ids:
            raise ValueError('Каталог пуст: генератору не из чего строить просмотры')
        self._films = list(film_ids)
        self._seed = seed
        self._users = users
        self._days = days
        self._rng = random.Random(seed)
        self._clusters = self._build_clusters()
        # Популярность внутри кластера: степенная, поэтому один-два фильма
        # собирают заметную долю просмотров кластера, а хвост — единицы.
        self._popularity = {film_id: (1.0 - self._rng.random() ** 0.35) for film_id in self._films}

    def _build_clusters(self) -> list[list[str]]:
        shuffled = list(self._films)
        self._rng.shuffle(shuffled)
        count = max(2, len(shuffled) // FILMS_PER_CLUSTER)
        clusters: list[list[str]] = [[] for _ in range(count)]
        for position, film_id in enumerate(shuffled):
            clusters[position % count].append(film_id)
        return clusters

    def _films_watched(self) -> int:
        roll = self._rng.random()
        cumulative = 0.0
        for share, (low, high) in ACTIVITY_PROFILE:
            cumulative += share
            if roll <= cumulative:
                return self._rng.randint(low, high)
        return ACTIVITY_PROFILE[-1][1][0]

    def _pick_film(self, favourites: list[str]) -> str:
        pool = self._films if self._rng.random() < EXPLORATION_RATE or not favourites else favourites
        # Отбор с весом популярности: два кандидата, побеждает более популярный.
        # Дёшево (два обращения вместо построения кумулятивных весов на каждый
        # выбор) и даёт ровно тот перекос, который нужен.
        first = self._rng.choice(pool)
        second = self._rng.choice(pool)
        return first if self._popularity[first] >= self._popularity[second] else second

    def sessions(self) -> Iterator[ViewSession]:
        """Все сеансы просмотра, по пользователям."""
        now = datetime.datetime.now(datetime.UTC)
        window = datetime.timedelta(days=self._days)

        for index in range(self._users):
            user_id = user_uuid(index, self._seed)
            favourites: list[str] = []
            for cluster in self._rng.sample(self._clusters, k=min(2, len(self._clusters))):
                favourites.extend(cluster)

            seen: set[str] = set()
            for _ in range(self._films_watched()):
                film_id = self._pick_film(favourites)
                if film_id in seen:
                    continue
                seen.add(film_id)

                started_at = now - window * self._rng.random()
                duration_ms = self._rng.randint(*DURATION_RANGE_MS)
                # Кривая досматриваемости: большинство либо бросает в начале,
                # либо досматривает. Равномерная доля дала бы ровную кривую,
                # какой не бывает ни у одного сервиса.
                roll = self._rng.random()
                if roll < 0.35:
                    completion = self._rng.uniform(0.02, 0.25)
                elif roll < 0.55:
                    completion = self._rng.uniform(0.25, 0.75)
                else:
                    completion = self._rng.uniform(0.9, 1.0)

                yield ViewSession(
                    user_id=user_id,
                    film_id=film_id,
                    session_id=f'synthetic-{self._seed}-{index}',
                    started_at=started_at,
                    duration_ms=duration_ms,
                    completion_rate=round(min(completion, 1.0), 4),
                    quality=self._rng.choice(QUALITIES),
                    device_type=self._rng.choice(DEVICE_TYPES),
                )
