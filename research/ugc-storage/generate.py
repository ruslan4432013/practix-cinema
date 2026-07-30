"""
Генерация датасета исследования в выбранное хранилище.

Один и тот же детерминированный набор данных заливается в MongoDB, PostgreSQL и
ClickHouse, чтобы бенчмарк мерил три хранилища на одинаковых данных, а не на
трёх похожих.

Запуск (стенд должен быть поднят, см. README.md):

    uv run --with pymongo --with faker \
        python research/ugc-storage/generate.py --store mongo
    uv run --with psycopg2-binary --with faker \
        python research/ugc-storage/generate.py --store postgres
    uv run --with clickhouse-connect --with faker \
        python research/ugc-storage/generate.py --store clickhouse

Сначала прогоняйте `--scale smoke` (1/100 объёма): он проверяет, что стенд,
схема и драйвер живы, за минуту вместо десятков минут.

Флаги:
    --store mongo|postgres|clickhouse   обязательный
    --scale full|smoke                  объём (по умолчанию full)
    --batch N                           размер пачки вставки (по умолчанию 10000)
    --seed N                            зерно генератора (по умолчанию 42)
    --keep                              не очищать хранилище перед заливкой
    --skip-optimize                     пропустить VACUUM ANALYZE / OPTIMIZE FINAL
"""

from __future__ import annotations

import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.cli import bootstrap, human
from lib.dataset import build_body_pool
from lib.store import chunked

USAGE = __doc__ or ''

# Гистограмма преагрегата собирается на стороне генератора, а не запросом к
# хранилищу: три диалекта агрегации ради одной и той же арифметики — это три
# места, где можно ошибиться по-разному. В бою преагрегат ведётся инкрементально
# на записи (см. w1_set_rating) плюс периодический пересчёт.
HIST_SIZE = 11


class FilmRatingAccumulator:
    """Счётчики рейтинга по фильмам, собираемые попутно с заливкой лайков."""

    def __init__(self) -> None:
        self._data: dict[uuid.UUID, list[int]] = {}

    def add(self, film_id: uuid.UUID, rating: int) -> None:
        bucket = self._data.get(film_id)
        if bucket is None:
            bucket = [0, 0, *([0] * HIST_SIZE)]
            self._data[film_id] = bucket
        bucket[0] += 1
        bucket[1] += rating
        bucket[2 + rating] += 1

    def rows(self) -> Iterator[dict]:
        for film_id, bucket in self._data.items():
            yield {
                'film_id': film_id,
                'ratings_count': bucket[0],
                'ratings_sum': bucket[1],
                'hist': bucket[2:],
            }

    def __len__(self) -> int:
        return len(self._data)


def load(
    label: str,
    rows: Iterator[dict],
    writer,
    batch_size: int,
    expected: int,
    accumulator: FilmRatingAccumulator | None = None,
) -> float:
    started = time.perf_counter()
    written = 0
    reported = 0
    for batch in chunked(rows, batch_size):
        if accumulator is not None:
            for row in batch:
                accumulator.add(row['film_id'], row['rating'])
        writer(batch)
        written += len(batch)
        if written - reported >= max(batch_size * 10, 50_000):
            reported = written
            elapsed = time.perf_counter() - started
            # flush: строки прогресса нужны, пока заливка идёт, а stdout в
            # пайпе буферизуется поблочно и показал бы их все разом в конце.
            print(
                f'    {label}: {human(written)} / {human(expected)} ({human(written / elapsed)} строк/с)',
                flush=True,
            )
    elapsed = time.perf_counter() - started
    rate = written / elapsed if elapsed else 0
    print(f'  {label}: {human(written)} строк за {elapsed:.1f} с ({human(rate)} строк/с)', flush=True)
    return elapsed


def main() -> int:
    args, profile, dataset, store = bootstrap(
        {'store': '', 'scale': 'full', 'batch': 10_000, 'seed': 42, 'keep': False, 'skip_optimize': False},
        USAGE,
    )
    batch_size = args['batch']

    print(f'Хранилище: {store.name}')
    print(f'Профиль: {profile.name} — {human(profile.likes)} лайков, ', end='')
    print(f'{human(profile.bookmarks)} закладок, {human(profile.reviews)} рецензий')
    print(f'Пользователей: {human(profile.users)}, фильмов: {human(profile.films)}, seed: {args["seed"]}')
    print()

    started = time.perf_counter()
    try:
        if not args['keep']:
            print('Очистка хранилища...')
            store.truncate()

        accumulator = FilmRatingAccumulator()
        load('лайки', dataset.iter_likes(), store.write_likes, batch_size, profile.likes, accumulator)
        load('закладки', dataset.iter_bookmarks(), store.write_bookmarks, batch_size, profile.bookmarks)

        print('  тексты рецензий: собираем пул...')
        body_pool = build_body_pool(seed=args['seed'])
        load('рецензии', dataset.iter_reviews(body_pool), store.write_reviews, batch_size, profile.reviews)
        load(
            'голоса за рецензии',
            dataset.iter_review_votes(),
            store.write_review_votes,
            batch_size,
            profile.review_votes,
        )
        load('преагрегат рейтинга', accumulator.rows(), store.write_film_rating, batch_size, len(accumulator))

        if not args['skip_optimize']:
            # Без этого шага сравнение нечестное: PostgreSQL без ANALYZE
            # планирует по статистике пустых таблиц, а ClickHouse читал бы
            # несхлопнутые куски. Каждому хранилищу даётся его лучший расклад.
            print('Приводим хранилище в «прогретое» состояние (это может занять минуты)...')
            optimize_started = time.perf_counter()
            store.optimize()
            print(f'  готово за {time.perf_counter() - optimize_started:.1f} с')

        print()
        print('Итог:')
        for name, count in store.counts().items():
            print(f'  {name:<16} {human(count):>14}')
        print(f'\nВсего: {time.perf_counter() - started:.1f} с')
    finally:
        store.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
