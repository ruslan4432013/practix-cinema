"""Метрики качества — на примерах, посчитанных на бумаге.

Арифметику метрик легко испортить незаметно: поделить на длину выдачи вместо k,
посчитать покрытие по выдаче вместо каталога, тихо выкинуть пользователей, для
которых модель ничего не предложила. Ошибка при этом не падает, а делает числа
красивее — единственная защита от неё в том, чтобы ответ был известен заранее.
"""

import pytest

from practix_recsys_trainer.metrics.quality import (
    catalog_coverage,
    evaluate,
    format_table,
    precision_at_k,
    recall_at_k,
)


def test_precision_divides_by_k_not_by_the_length_of_the_answer():
    """Иначе одна угаданная позиция дала бы precision@10 = 1.0.

    Модель, вернувшая один правильный ответ, обошла бы модель, вернувшую десять
    и угадавшую пять, — то есть метрика вознаграждала бы молчание.
    """
    assert precision_at_k(['a'], {'a', 'b'}, k=10) == pytest.approx(0.1)
    assert precision_at_k(['a', 'b', 'x', 'y', 'z'], {'a', 'b'}, k=10) == pytest.approx(0.2)


def test_precision_looks_only_at_the_first_k_positions():
    assert precision_at_k(['x', 'x', 'a'], {'a'}, k=2) == pytest.approx(0.0)


def test_recall_divides_by_the_relevant_set():
    assert recall_at_k(['a', 'x'], {'a', 'b', 'c', 'd'}, k=10) == pytest.approx(0.25)


def test_recall_of_an_empty_relevant_set_is_zero_not_a_division_error():
    assert recall_at_k(['a'], set(), k=10) == 0.0


def test_coverage_is_measured_against_the_catalog():
    """По выдаче покрытие всегда равнялось бы единице и не значило бы ничего.

    Смысл метрики — вытаскиваем ли мы длинный хвост: SLO требует, чтобы блок
    похожих был непуст хотя бы у 60% каталога.
    """
    assert catalog_coverage({'u1': ['a', 'b'], 'u2': ['b', 'c']}, catalog_size=10) == pytest.approx(0.3)


def test_coverage_of_an_empty_catalog_is_zero():
    assert catalog_coverage({'u1': ['a']}, catalog_size=0) == 0.0


def test_users_the_model_ignored_count_as_misses():
    """Пропустить их значило бы мерить качество только там, где модель сработала.

    Молчание — тоже промах: человек, которому не показали ничего, не получил
    рекомендаций ровно так же, как человек, которому показали мимо.
    """
    report = evaluate(
        'модель',
        {'u1': ['a']},
        {'u1': {'a'}, 'u2': {'b'}},
        k=1,
        catalog_size=10,
    )

    assert report.users_evaluated == 2
    assert report.precision == pytest.approx(0.5)


def test_users_without_relevant_items_are_excluded_from_the_average():
    """Ноль здесь означал бы промах, хотя промахиваться было не по чему."""
    report = evaluate('модель', {'u1': ['a']}, {'u1': {'a'}, 'u2': set()}, k=1, catalog_size=10)

    assert report.users_evaluated == 1
    assert report.precision == pytest.approx(1.0)


def test_empty_evaluation_returns_zeroes_instead_of_dividing_by_zero():
    report = evaluate('модель', {}, {}, k=10, catalog_size=100)

    assert report.users_evaluated == 0
    assert report.precision == 0.0
    assert report.recall == 0.0


def test_table_puts_every_model_on_its_own_row():
    baseline = evaluate('popular (baseline)', {'u1': ['a']}, {'u1': {'a'}}, k=1, catalog_size=4)
    model = evaluate('cooccurrence', {'u1': ['b']}, {'u1': {'a'}}, k=1, catalog_size=4)

    table = format_table([baseline, model])

    assert 'popular (baseline)' in table
    assert 'cooccurrence' in table
    assert len(table.splitlines()) == 4
