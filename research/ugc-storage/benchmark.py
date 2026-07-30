"""
Замеры чтения и записи по сценариям исследования.

Отвечает на вопрос задания «укладываемся ли в 200 мс» — по p99, а не по
среднему: среднее прячет ровно тот хвост, из-за которого пользователь видит
подвисание.

Запуск (данные должны быть залиты generate.py):

    uv run --with pymongo \
        python research/ugc-storage/benchmark.py --store mongo
    uv run --with psycopg2-binary \
        python research/ugc-storage/benchmark.py --store postgres
    uv run --with clickhouse-connect \
        python research/ugc-storage/benchmark.py --store clickhouse

Флаги:
    --store mongo|postgres|clickhouse   обязательный
    --scale full|smoke                  профиль данных (должен совпадать с заливкой)
    --iterations N                      измерений на сценарий (по умолчанию 2000)
    --concurrency N                     параллельных клиентов (по умолчанию 1)
    --seed N                            зерно генератора (по умолчанию 42)
    --out PATH                          куда положить JSON (по умолчанию results/)

Сценарии чтения выбирают активных пользователей и популярные фильмы: у
пользователя с двумя лайками любой запрос быстр в любом хранилище, и такое
измерение ничего не различает.
"""

from __future__ import annotations

import json
import platform
import random
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.cli import bootstrap, human
from lib.dataset import film_uuid, user_uuid
from lib.stats import Measurement, print_table, summarize
from lib.store import READ_SCENARIOS, WRITE_SCENARIOS

USAGE = __doc__ or ''

# Сколько ждать появления записи в сценарии W2, прежде чем считать её потерянной.
VISIBILITY_TIMEOUT_S = 2.0


def measure(
    scenario: str,
    description: str,
    make_call: Callable[[int], Callable[[], object]],
    iterations: int,
    concurrency: int,
) -> Measurement:
    """Прогон одного сценария.

    Первые 10% измерений отбрасываются: они оплачивают установку соединений и
    прогрев кеша страниц, и без этого первый сценарий в списке всегда выглядел
    бы худшим независимо от того, что он делает.
    """
    warmup = max(1, iterations // 10)
    durations: list[float] = []
    errors = 0

    def run_one(index: int) -> tuple[float, bool]:
        call = make_call(index)
        started = time.perf_counter()
        try:
            call()
        except Exception:  # noqa: BLE001 - любая ошибка драйвера здесь равнозначна
            return (time.perf_counter() - started) * 1000, False
        return (time.perf_counter() - started) * 1000, True

    if concurrency == 1:
        for index in range(warmup):
            run_one(-index - 1)
        wall_started = time.perf_counter()
        for index in range(iterations):
            elapsed, ok = run_one(index)
            durations.append(elapsed)
            errors += 0 if ok else 1
        wall = time.perf_counter() - wall_started
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            list(pool.map(run_one, range(-warmup, 0)))
            wall_started = time.perf_counter()
            for elapsed, ok in pool.map(run_one, range(iterations)):
                durations.append(elapsed)
                errors += 0 if ok else 1
            wall = time.perf_counter() - wall_started

    result = summarize(scenario, description, durations, wall, errors)
    flag = 'ERR' if result.errors else ('ok ' if result.within_sla else 'SLA')
    print(
        f'  [{flag}] {scenario:<10} p50={result.p50_ms:>8.2f}  p95={result.p95_ms:>9.2f}  '
        f'p99={result.p99_ms:>9.2f} мс  ошибок={result.errors}'
    )
    return result


def main() -> int:
    args, profile, dataset, store = bootstrap(
        {'store': '', 'scale': 'full', 'iterations': 2_000, 'concurrency': 1, 'seed': 42, 'out': ''},
        USAGE,
    )
    iterations = args['iterations']
    concurrency = args['concurrency']

    print(f'Хранилище: {store.name}, профиль: {profile.name}, ', end='')
    print(f'измерений на сценарий: {human(iterations)}, параллельность: {concurrency}')

    print('Готовим выборки идентификаторов...')
    active = dataset.active_users()
    if not active:
        raise SystemExit('В профиле нет активных пользователей — данные не залиты?')
    films = dataset.sample_films(max(iterations, 1000))
    rng = random.Random(args['seed'] * 977)
    print(f'  активных пользователей: {human(len(active))}, фильмов в выборке: {human(len(set(films)))}')

    # Пользователи за границей сгенерированного диапазона: каждая запись в
    # сценарии W1 — гарантированно вставка, а не попадание в уже существующую
    # пару. Иначе часть измерений мерила бы обновление, часть — вставку, и
    # перцентили смешали бы два разных пути.
    #
    # `salt` разводит сценарии между собой, а отрицательные индексы (прогрев) —
    # в отдельную полосу: иначе прогрев занял бы те же пары, и первая же
    # измеряемая вставка оказалась бы обновлением.
    def fresh_user(index: int, salt: int = 0):
        band = 0 if index >= 0 else 4_000_000
        return user_uuid(profile.users + salt + band + abs(index))

    def a_user(index: int):
        return user_uuid(active[abs(index) % len(active)])

    def a_film(index: int):
        return film_uuid(films[abs(index) % len(films)])

    measurements: list[Measurement] = []
    try:
        print('\nЧтение:')
        read_calls: dict[str, Callable[[int], Callable[[], object]]] = {
            'R1': lambda i: (lambda: store.r1_user_liked_films(a_user(i))),
            'R2': lambda i: (lambda: store.r2_film_like_counts(a_film(i))),
            'R3': lambda i: (lambda: store.r3_film_avg_rating(a_film(i))),
            'R3c': lambda i: (lambda: store.r3c_film_avg_cached(a_film(i))),
            'R4': lambda i: (lambda: store.r4_user_bookmarks(a_user(i))),
            'R5-new': lambda i: (lambda: store.r5_film_reviews(a_film(i), 'new')),
            'R5-useful': lambda i: (lambda: store.r5_film_reviews(a_film(i), 'useful')),
            'R5-rating': lambda i: (lambda: store.r5_film_reviews(a_film(i), 'rating')),
        }
        for code, description in READ_SCENARIOS:
            measurements.append(measure(code, description, read_calls[code], iterations, concurrency))

        print('\nЗапись и чтение в реальном времени:')

        def insert_like(index: int) -> Callable[[], object]:
            user, film = fresh_user(index), a_film(index)
            rating = rng.randint(0, 10)
            return lambda: store.w1_set_rating(user, film, rating)

        def update_like(index: int) -> Callable[[], object]:
            # Обновление существующей оценки: пользователь из активных, фильм —
            # из тех, что он точно оценивал, взять неоткуда без запроса к базе,
            # поэтому изменение может оказаться и вставкой. Для пути «обновление»
            # это допустимо: важен upsert как операция, а не гарантия попадания.
            user, film = a_user(index), a_film(index)
            rating = rng.randint(0, 10)
            return lambda: store.w1_set_rating(user, film, rating)

        def visible_write(index: int) -> Callable[[], object]:
            user, film = fresh_user(index, salt=10_000_000), a_film(index)
            rating = rng.randint(0, 10)
            before = store.r3c_film_avg_cached(film)[1]

            def call() -> object:
                store.w1_set_rating(user, film, rating)
                deadline = time.perf_counter() + VISIBILITY_TIMEOUT_S
                while time.perf_counter() < deadline:
                    if store.r3c_film_avg_cached(film)[1] > before:
                        return True
                raise TimeoutError('оценка не появилась в преагрегате')

            return call

        write_calls = {'W1': insert_like, 'W1u': update_like, 'W2': visible_write}
        for code, description in WRITE_SCENARIOS:
            measurements.append(measure(code, description, write_calls[code], iterations, concurrency))

        print_table(measurements)

        report = {
            'store': store.name,
            'profile': profile.__dict__,
            'iterations': iterations,
            'concurrency': concurrency,
            'seed': args['seed'],
            'measured_at': datetime.now(UTC).isoformat(timespec='seconds'),
            'platform': f'{platform.system()} {platform.release()} / {platform.machine()}',
            'python': platform.python_version(),
            'measurements': [m.as_dict() for m in measurements],
        }
        out_path = (
            Path(args['out'])
            if args['out']
            else Path(__file__).parent / 'results' / f'{store.name}-{profile.name}.json'
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'Результаты: {out_path}')
    finally:
        store.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
