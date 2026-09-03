"""Запросы к ClickHouse: какой конец выборки отрезает LIMIT.

Живой ClickHouse здесь не нужен — проверяется текст запроса и разбор ответа.
Функциональный набор это свойство не видит и увидеть не может: там у всех
посеянных сессий одинаковый ``started_at``, а лимит в пять миллионов
недостижим на дюжине пользователей. То есть регрессия прошла бы весь CI зелёной.
"""

import datetime

from practix_recsys_trainer.core.config import settings
from practix_recsys_trainer.sources.clickhouse import fetch_interactions

NOW = datetime.datetime(2026, 8, 30, 12, 0, tzinfo=datetime.UTC)


class FakeResult:
    def __init__(self, rows: list[tuple]) -> None:
        self.result_rows = rows


class FakeClient:
    """Записывает запрос вместо того, чтобы его выполнять."""

    def __init__(self, rows: list[tuple] | None = None) -> None:
        self.rows = rows or []
        self.query_text = ''
        self.parameters: dict[str, object] = {}

    def query(self, query: str, parameters: dict[str, object] | None = None) -> FakeResult:
        self.query_text = query
        self.parameters = parameters or {}
        return FakeResult(self.rows)


def test_limit_cuts_the_old_tail_and_not_the_fresh_head():
    """Убывающая сортировка — единственное, что делает лимит предохранителем.

    По возрастанию ``LIMIT`` оставлял бы самый старый срез истории: модель
    училась бы на вкусах, которые давно сменились, а сплит по времени назвал бы
    «будущим» события из прошлого. Ни лога, ни падения при этом не будет —
    поэтому свойство закреплено тестом, а не комментарием.
    """
    client = FakeClient()

    fetch_interactions(client)

    assert 'ORDER BY last_seen DESC' in client.query_text


def test_ordering_has_a_tiebreaker_so_the_cut_is_reproducible():
    """На синтетике совпадающие метки времени — норма, а не редкость.

    Без вторичного ключа граница отсечения зависела бы от порядка выдачи
    ClickHouse, и два прогона на одних и тех же данных дали бы разные выборки.
    """
    client = FakeClient()

    fetch_interactions(client)

    assert 'ORDER BY last_seen DESC, user_id, film_id' in client.query_text


def test_limit_comes_from_the_setting_that_documents_it():
    client = FakeClient()

    fetch_interactions(client)

    assert 'LIMIT %(limit)s' in client.query_text
    assert client.parameters['limit'] == settings.RECS_TRAINER_MAX_INTERACTIONS


def test_since_narrows_the_window_and_is_passed_as_a_parameter():
    """Время в тексте запроса было бы склейкой строк — параметр им не является."""
    client = FakeClient()

    fetch_interactions(client, since=NOW)

    assert 'event_time >= %(since)s' in client.query_text
    assert client.parameters['since'] == NOW


def test_rows_become_interactions_with_a_numeric_weight():
    """Драйвер возвращает Decimal для агрегатов — модели ждут float."""
    client = FakeClient([('u1', 'f1', 1, NOW)])

    interactions = fetch_interactions(client)

    assert len(interactions) == 1
    assert interactions[0].user_id == 'u1'
    assert interactions[0].film_id == 'f1'
    assert isinstance(interactions[0].weight, float)
    assert interactions[0].last_seen == NOW
