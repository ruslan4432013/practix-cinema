"""Сплит по времени (T2.4) и детерминизм генератора синтетики (T2.3)."""

import datetime
import uuid

from practix_recsys_trainer.core.config import settings
from practix_recsys_trainer.dataset.sinks import film_view_rows
from practix_recsys_trainer.dataset.split import group_by_user, split_by_time
from practix_recsys_trainer.dataset.synthetic import Generator
from practix_recsys_trainer.metrics.evaluation import evaluate_models
from practix_recsys_trainer.sources.clickhouse import Interaction

NOW = datetime.datetime(2026, 8, 30, 12, 0, tzinfo=datetime.UTC)
FILMS = [str(uuid.UUID(int=index)) for index in range(120)]


def view(user: str, film: str, days_ago: int) -> Interaction:
    return Interaction(user, film, 1.0, NOW - datetime.timedelta(days=days_ago))


def test_training_part_never_contains_anything_newer_than_the_test_part():
    """Главное свойство сплита, ради которого он не случайный.

    Случайный сплит разрешает модели учиться на том, что человек посмотрит
    завтра, и предсказывать вчерашнее. Метрики выходят красивыми, а на проде
    качество оказывается вдвое ниже отчётного.
    """
    interactions = [view(f'u{index}', f'f{index}', days_ago=index) for index in range(10)]

    split = split_by_time(interactions, test_fraction=0.3)

    assert max(item.last_seen for item in split.train) <= min(item.last_seen for item in split.test)


def test_split_is_reproducible_when_timestamps_repeat():
    """На синтетике одинаковые метки времени — норма, а не редкость.

    Без вторичного ключа сортировки порядок зависел бы от порядка выдачи
    ClickHouse, и сплит перестал бы быть воспроизводимым.
    """
    interactions = [view('u2', 'fB', 1), view('u1', 'fA', 1), view('u1', 'fB', 1), view('u2', 'fA', 1)]

    first = split_by_time(interactions, test_fraction=0.5)
    second = split_by_time(list(reversed(interactions)), test_fraction=0.5)

    assert [(item.user_id, item.film_id) for item in first.test] == [
        (item.user_id, item.film_id) for item in second.test
    ]


def test_empty_test_part_is_reported_as_unusable_rather_than_measured():
    """Метрики по одному наблюдению честнее не считать, чем напечатать 1.0."""
    assert not split_by_time([], test_fraction=0.2).is_usable
    assert not split_by_time([view('u1', 'fA', 1)], test_fraction=0.0).is_usable


def test_group_by_user_collapses_repeats():
    grouped = group_by_user([view('u1', 'fA', 1), view('u1', 'fA', 2), view('u1', 'fB', 3)])

    assert grouped == {'u1': {'fA', 'fB'}}


def test_same_seed_gives_byte_for_byte_the_same_history():
    """Прогон, который нельзя повторить, нельзя и отладить."""
    first = list(Generator(FILMS, seed=7, users=40, days=30).sessions())
    second = list(Generator(FILMS, seed=7, users=40, days=30).sessions())

    assert [(item.user_id, item.film_id, item.completion_rate) for item in first] == [
        (item.user_id, item.film_id, item.completion_rate) for item in second
    ]


def test_different_seeds_give_different_audiences():
    first = {item.user_id for item in Generator(FILMS, seed=1, users=20, days=30).sessions()}
    second = {item.user_id for item in Generator(FILMS, seed=2, users=20, days=30).sessions()}

    assert not first & second


def test_generated_history_lets_the_model_beat_the_popular_baseline(monkeypatch):
    """Ради этого свойства в генераторе и заведены вкусовые кластеры.

    При равномерно случайных просмотрах у любых двух фильмов будет примерно
    одинаковое пересечение аудитории, со-встречаемость выродится в шум, и E5
    покажет, что модель не лучше baseline. Это была бы правда о генераторе, а не
    о модели, — и весь сквозной прогон превратился бы в проверку самого себя.

    Поэтому проверяется не косвенный признак структуры (перекос популярности), а
    прямо то, что от данных требуется: на них должно быть что выучить.

    ALS выключен: здесь проверяется генератор, а не E6, и лишние секунды на
    факторизацию юнит-тесту ни к чему.
    """
    monkeypatch.setattr(settings, 'RECS_TRAINER_ALS_ENABLED', False)

    sessions = list(Generator(FILMS, seed=3, users=400, days=30).sessions())
    interactions = [
        Interaction(item.user_id, item.film_id, item.completion_rate, item.started_at)
        for item in sessions
        if item.completion_rate >= settings.RECS_TRAINER_MIN_COMPLETION
    ]

    reports = {report.name: report for report in evaluate_models(interactions, {}, k=10, catalog_size=len(FILMS))}

    baseline = reports['popular (baseline)']
    model = reports['cooccurrence']
    assert model.precision > baseline.precision
    # И вытаскивает длинный хвост: популярное по построению показывает десять
    # фильмов всем, со-встречаемость обязана покрывать заметную долю каталога.
    assert model.coverage > baseline.coverage


def test_generator_refuses_to_work_without_a_catalog():
    """Выдуманные UUID дали бы рекомендации на несуществующие фильмы."""
    try:
        Generator([], seed=1, users=1, days=1)
    except ValueError as exc:
        assert 'Каталог пуст' in str(exc)
    else:
        raise AssertionError('генератор обязан отказаться работать по пустому каталогу')


def test_session_rows_carry_the_real_film_id_and_a_stable_view_id():
    session = next(iter(Generator(FILMS, seed=5, users=1, days=10).sessions()))

    rows = list(film_view_rows(session))

    assert rows, 'сеанс обязан породить хотя бы одну метку прогресса'
    assert all(str(row[2]) == session.film_id for row in rows)
    # view_id один на весь сеанс: по нему считаются просмотры, а не события.
    assert {row[5] for row in rows} == {session.view_id}


def test_completed_session_emits_a_completion_event():
    sessions = list(Generator(FILMS, seed=11, users=60, days=30).sessions())
    completed = next(item for item in sessions if item.completed)

    types = [row[1] for row in film_view_rows(completed)]

    assert 'video_completed' in types
