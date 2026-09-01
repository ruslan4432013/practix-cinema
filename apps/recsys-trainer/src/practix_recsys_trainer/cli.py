"""CLI офлайн-обучения. Каждая команда закрывает пункт DoD своего эпика.

Подкоманда пишется явно (пустой ``@cli.callback()``) — тот же приём, что в
``practix_auth.cli``: у Typer при единственной команде схлопывается имя, и
добавление второй молча меняет интерфейс всех вызовов.

Команды печатают в stdout, и это их назначение: человек запускает их руками и
читает ответ. Поэтому ``print`` здесь не нарушение, а интерфейс.
"""

import json
import logging
import sys

import typer

from practix_recsys_trainer.core.config import settings
from practix_recsys_trainer.core.observability import configure_logging

LOGGING = configure_logging()
logger = logging.getLogger(__name__)

cli = typer.Typer(help='Офлайн-обучение рекомендаций: данные, модели, витрина, метрики')


@cli.callback()
def _root() -> None:
    """Снимает схлопывание единственной команды у Typer."""


@cli.command()
def stats() -> None:
    """Сколько данных есть на самом деле (T2.1).

    Первая команда на новом стенде. Если активных пользователей единицы, обучать
    нечего, и узнать это надо до того, как четыре часа ушло на отладку модели.
    """
    from practix_recsys_trainer.sources import catalog, clickhouse, ratings

    client = clickhouse.connect()
    try:
        signal = clickhouse.fetch_stats(client)
    finally:
        client.close()

    catalog_size = len(catalog.fetch_film_ids())
    report = {
        'clickhouse': {
            'rows': signal.rows,
            'view_sessions': signal.views,
            'users': signal.users,
            'films': signal.films,
            'active_users': signal.active_users,
            'first_event': signal.first_event.isoformat() if signal.rows else None,
            'last_event': signal.last_event.isoformat() if signal.rows else None,
        },
        'ugc_db': {'ratings': ratings.count_ratings()},
        'catalog': {'films': catalog_size},
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if signal.active_users < 10:
        print(
            '\nАктивных пользователей мало: обучать почти не на чем.\n'
            'Заполнить стенд: python -m practix_recsys_trainer.cli generate --sink clickhouse',
            file=sys.stderr,
        )


@cli.command()
def generate(
    sink: str = typer.Option('clickhouse', help='clickhouse — массово и напрямую; api — через боевой путь коллектора'),
    users: int = typer.Option(2000, min=1, help='Сколько синтетических зрителей завести'),
    films: int = typer.Option(0, min=0, help='Ограничить каталог первыми N фильмами (0 — весь)'),
    days: int = typer.Option(60, min=1, help='За сколько последних дней размазать просмотры'),
    seed: int = typer.Option(settings.RECS_TRAINER_GENERATOR_SEED, help='Зерно генератора: тот же seed — те же данные'),
) -> None:
    """Синтетическая история просмотров (T2.3).

    Стенд поднимается с пустым ClickHouse, а без взаимодействий обучать нечего —
    раздел 8 ТЗ числит это риском номер один и закладывает генератор явно.
    """
    from practix_recsys_trainer.dataset import sinks
    from practix_recsys_trainer.dataset.synthetic import Generator
    from practix_recsys_trainer.sources import catalog, clickhouse

    film_ids = catalog.fetch_film_ids(limit=films or None)
    if not film_ids:
        raise typer.BadParameter('Каталог пуст: theatre-db недоступна или в ней нет фильмов')

    generator = Generator(film_ids, seed=seed, users=users, days=days)
    print(f'Каталог: {len(film_ids)} фильмов, зрителей: {users}, окно: {days} дней, seed: {seed}')

    if sink == 'clickhouse':
        client = clickhouse.connect()
        try:
            written = sinks.to_clickhouse(client, generator.sessions())
        finally:
            client.close()
        print(f'Записано строк в ugc.film_views: {written}')
    elif sink == 'api':
        accepted, rejected = sinks.to_api(generator.sessions())
        print(f'Отправлено через коллектор: принято {accepted}, отклонено {rejected}')
        print(
            'События уйдут в ClickHouse анонимными: user_id коллектор берёт только из токена. '
            'Этот режим проверяет живучесть стыка, а не наполняет обучающую выборку.'
        )
    else:
        raise typer.BadParameter(f'Неизвестный слив: {sink}. Допустимы clickhouse и api')


@cli.command()
def dataset(
    out: str = typer.Option('', help='Куда сложить матрицу (по умолчанию RECS_TRAINER_DATA_DIR)'),
) -> None:
    """Выгрузка матрицы «пользователь × фильм» одной командой (T2.2)."""
    from pathlib import Path

    import numpy as np
    from scipy import sparse

    from practix_recsys_trainer.dataset import matrix as matrix_builder
    from practix_recsys_trainer.dataset.split import split_by_time
    from practix_recsys_trainer.sources import clickhouse, ratings

    client = clickhouse.connect()
    try:
        interactions = clickhouse.fetch_interactions(client)
    finally:
        client.close()

    built = matrix_builder.build(
        interactions,
        ratings.fetch_ratings(),
        min_user_events=settings.RECS_TRAINER_MIN_USER_EVENTS,
        min_film_events=settings.RECS_TRAINER_MIN_FILM_EVENTS,
    )
    split = split_by_time(interactions)

    target = Path(out or settings.RECS_TRAINER_DATA_DIR)
    target.mkdir(parents=True, exist_ok=True)
    sparse.save_npz(target / 'matrix.npz', built.matrix)
    np.save(target / 'user_ids.npy', np.array(built.user_ids, dtype=object), allow_pickle=True)
    np.save(target / 'film_ids.npy', np.array(built.film_ids, dtype=object), allow_pickle=True)

    users, film_count = built.shape
    print(
        json.dumps(
            {
                'path': str(target),
                'users': users,
                'films': film_count,
                'interactions': built.nnz,
                # Сплит по ВРЕМЕНИ, а не случайный: случайный разрешил бы модели
                # учиться на будущем и завысил бы метрики (T2.4).
                'split': {'train': len(split.train), 'test': len(split.test)},
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@cli.command()
def train(
    run_key: str = typer.Option('', help='Ключ прогона; по умолчанию — текущий час (идемпотентность по слоту)'),
    with_metrics: bool = typer.Option(True, help='Считать метрики качества и класть их в stats версии'),
) -> None:
    """Полный прогон обучения и раскладка витрины (E3, E6)."""
    from practix_recsys_trainer.metrics.quality import format_table
    from practix_recsys_trainer.pipeline import run_training

    result = run_training(run_key=run_key or None, with_metrics=with_metrics)
    if result.skipped:
        print(f'Прогон {result.run_key} пропущен: версия на этот слот уже построена')
        return

    print(
        json.dumps({'version': result.version, 'models': result.models, **result.stats}, ensure_ascii=False, indent=2)
    )
    if result.reports:
        print()
        print(format_table(result.reports))


@cli.command()
def evaluate(
    k: int = typer.Option(10, min=1, max=100, help='Глубина выдачи, на которой считаются метрики'),
) -> None:
    """Качество моделей против baseline «просто популярное» (E5, F3.3)."""
    from practix_recsys_trainer.metrics.evaluation import evaluate_models
    from practix_recsys_trainer.metrics.quality import format_table
    from practix_recsys_trainer.sources import catalog, clickhouse, ratings

    client = clickhouse.connect()
    try:
        interactions = clickhouse.fetch_interactions(client)
    finally:
        client.close()

    reports = evaluate_models(
        interactions,
        ratings.fetch_ratings(),
        k=k,
        catalog_size=len(catalog.fetch_film_ids()),
    )
    if not reports:
        print('Мерить нечего: отложенная выборка пуста. Заполните стенд командой generate.')
        raise typer.Exit(code=1)

    print(format_table(reports))
    print()
    baseline = reports[0]
    for report in reports[1:]:
        delta = report.precision - baseline.precision
        verdict = 'лучше baseline' if delta > 0 else 'НЕ лучше baseline'
        print(f'{report.name}: precision@{k} {delta:+.4f} — {verdict}')


if __name__ == '__main__':
    cli()
