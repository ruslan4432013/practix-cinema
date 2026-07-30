"""Проверки эквивалентности извлечённого backoff прежним реализациям.

Смысл этих тестов — не «работает ли повтор», а «совпадает ли КРИВАЯ задержки с
той, что была до извлечения». Дублировалась именно арифметика, и молча
разъехаться она может только здесь.
"""

import pytest

from practix_core.backoff import compute_delay, retry


def _legacy_etl_delays(count: int, start: float = 0.1, factor: int = 2, border: float = 10) -> list[float]:
    """Дословная копия цикла из прежнего ``etl/lib/backoff.py``."""
    delays: list[float] = []
    n = 0
    for _ in range(count):
        sleep_time = start * (factor**n)
        if sleep_time >= border:
            sleep_time = border
        else:
            n += 1
        delays.append(sleep_time)
    return delays


def _new_delays(count: int, start: float = 0.1, factor: int = 2, border: float = 10) -> list[float]:
    """Тот же порядок обновления счётчика, но через ``compute_delay``."""
    delays: list[float] = []
    n = 0
    for _ in range(count):
        sleep_time = compute_delay(n, start=start, factor=factor, border=border)
        if sleep_time < border:
            n += 1
        delays.append(sleep_time)
    return delays


def test_delay_curve_matches_legacy_etl_implementation():
    assert _new_delays(20) == _legacy_etl_delays(20)


@pytest.mark.parametrize(
    ('start', 'factor', 'border'),
    [(0.1, 2, 10), (0.5, 3, 30), (1.0, 2, 1.0), (0.01, 2, 0.05)],
)
def test_delay_curve_matches_for_other_parameters(start, factor, border):
    assert _new_delays(15, start, factor, border) == _legacy_etl_delays(15, start, factor, border)


def test_delay_is_capped_at_border():
    assert compute_delay(100, start=0.1, factor=2, border=10) == 10


def test_no_jitter_by_default():
    """Джиттер не должен появиться «в подарок» у синхронных потребителей."""
    values = {compute_delay(3, start=0.1, factor=2, border=10) for _ in range(50)}
    assert values == {0.8}


def test_jitter_stays_within_requested_ratio():
    values = [compute_delay(3, start=0.1, factor=2, border=10, jitter=0.2) for _ in range(200)]
    assert all(0.8 * 0.8 <= v <= 0.8 * 1.2 for v in values)
    assert len(set(values)) > 1


def test_retry_returns_result_after_transient_failures():
    calls = {'n': 0}

    @retry(start_sleep_time=0, border_sleep_time=0, max_attempts=5)
    def flaky():
        calls['n'] += 1
        if calls['n'] < 3:
            raise ValueError('not yet')
        return 'ok'

    assert flaky() == 'ok'
    assert calls['n'] == 3


def test_retry_raises_last_exception_when_attempts_exhausted():
    calls = {'n': 0}

    @retry(start_sleep_time=0, border_sleep_time=0, max_attempts=3)
    def always_fails():
        calls['n'] += 1
        raise ValueError('nope')

    with pytest.raises(ValueError, match='nope'):
        always_fails()
    assert calls['n'] == 3


def test_retry_with_max_attempts_none_keeps_retrying():
    """``None`` сохраняет бесконечное поведение прежнего etl/lib/backoff.py."""
    calls = {'n': 0}

    @retry(start_sleep_time=0, border_sleep_time=0, max_attempts=None)
    def eventually():
        calls['n'] += 1
        if calls['n'] < 50:
            raise ValueError('again')
        return 'done'

    assert eventually() == 'done'
    assert calls['n'] == 50


def test_retry_only_catches_listed_exceptions():
    @retry(start_sleep_time=0, border_sleep_time=0, max_attempts=5, exceptions=(ValueError,))
    def raises_type_error():
        raise TypeError('unrelated')

    with pytest.raises(TypeError, match='unrelated'):
        raises_type_error()
