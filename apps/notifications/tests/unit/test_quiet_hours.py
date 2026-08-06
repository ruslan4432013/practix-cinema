"""Тихие часы: окно через полночь и таймзона получателя."""

from datetime import UTC, datetime

import pytest

from practix_notifications.services import quiet_hours

START = quiet_hours.parse_time('22:00')
END = quiet_hours.parse_time('09:00')


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


@pytest.mark.parametrize(
    ('hour_utc', 'expected'),
    [
        (20, True),  # 23:00 по Москве — ночь
        (2, True),  # 05:00 по Москве — ночь
        (7, False),  # 10:00 по Москве — можно
        (18, False),  # 21:00 по Москве — можно
    ],
)
def test_window_wraps_midnight(hour_utc: int, expected: bool):
    assert quiet_hours.is_quiet(utc(2026, 8, 5, hour_utc), timezone='Europe/Moscow', start=START, end=END) is expected


def test_timezone_of_recipient_decides_not_of_server():
    """Один и тот же момент — ночь для одного получателя и день для другого."""
    moment = utc(2026, 8, 5, 20)
    assert quiet_hours.is_quiet(moment, timezone='Europe/Moscow', start=START, end=END) is True
    assert quiet_hours.is_quiet(moment, timezone='America/New_York', start=START, end=END) is False


def test_boundaries_are_inclusive_at_start_exclusive_at_end():
    assert quiet_hours.is_quiet(utc(2026, 8, 5, 22), timezone='UTC', start=START, end=END) is True
    assert quiet_hours.is_quiet(utc(2026, 8, 5, 9), timezone='UTC', start=START, end=END) is False


def test_equal_bounds_disable_the_window():
    same = quiet_hours.parse_time('00:00')
    assert quiet_hours.is_quiet(utc(2026, 8, 5, 3), timezone='UTC', start=same, end=same) is False


def test_next_allowed_is_end_of_window():
    moment = utc(2026, 8, 5, 23)
    assert quiet_hours.next_allowed_at(moment, timezone='UTC', start=START, end=END) == utc(2026, 8, 6, 9)


def test_next_allowed_returns_now_when_not_quiet():
    moment = utc(2026, 8, 5, 12)
    assert quiet_hours.next_allowed_at(moment, timezone='UTC', start=START, end=END) == moment


def test_bad_time_format_rejected():
    with pytest.raises(quiet_hours.QuietHoursError):
        quiet_hours.parse_time('22 часа')


def test_naive_datetime_rejected():
    with pytest.raises(quiet_hours.QuietHoursError):
        quiet_hours.is_quiet(datetime(2026, 8, 5, 23), timezone='UTC', start=START, end=END)
