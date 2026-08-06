"""Арифметика расписаний: пятница, Новый год, DST и поведение после простоя."""

from datetime import UTC, datetime, timedelta

import pytest

from practix_notifications.services import scheduling

MOSCOW = 'Europe/Moscow'


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_every_friday_noon_in_moscow():
    """«0 12 * * 5» — каждую пятницу в полдень ПО МОСКВЕ, а не по UTC."""
    # 5 августа 2026 — среда.
    nxt = scheduling.next_run_at('0 12 * * 5', after=utc(2026, 8, 5, 10), timezone=MOSCOW)
    assert nxt.weekday() == 4
    # Москва — UTC+3, значит полдень по Москве это 09:00 UTC.
    assert (nxt.hour, nxt.minute) == (9, 0)


def test_every_new_year():
    nxt = scheduling.next_run_at('0 10 1 1 *', after=utc(2026, 8, 5), timezone='UTC')
    assert (nxt.year, nxt.month, nxt.day, nxt.hour) == (2027, 1, 1, 10)


def test_deferred_is_absolute_moment():
    now = utc(2026, 8, 5, 12)
    assert scheduling.deferred_run_at(now, 3) == utc(2026, 8, 5, 15)


def test_deferred_rejects_non_positive():
    with pytest.raises(scheduling.ScheduleError):
        scheduling.deferred_run_at(utc(2026, 8, 5), 0)


def test_naive_datetime_rejected():
    """Наивное время неотличимо от чужого часового пояса — молчать нельзя."""
    with pytest.raises(scheduling.ScheduleError):
        scheduling.next_run_at('0 12 * * 5', after=datetime(2026, 8, 5, 10), timezone=MOSCOW)


def test_dst_forward_skips_missing_hour():
    """В таймзоне с переводом часов слот в «пропавшем» часе не наступает дважды."""
    # США переводят часы 8 марта 2026: 02:00 -> 03:00.
    slots = scheduling.slots_between(
        '30 2 * * *', after=utc(2026, 3, 7, 0), until=utc(2026, 3, 10, 0), timezone='America/New_York'
    )
    assert len(slots) == len(set(slots))


class TestCatchup:
    """Требование: после простоя не должны дублироваться старые и новые события."""

    CRON = '0 * * * *'  # каждый час

    def test_without_catchup_only_last_missed_slot_fires(self):
        due = utc(2026, 8, 5, 10)
        now = utc(2026, 8, 5, 13, 30)
        slots, following = scheduling.plan_recurring(self.CRON, timezone='UTC', due_at=due, now=now, catchup=False)
        assert slots == [utc(2026, 8, 5, 13)]
        assert following == utc(2026, 8, 5, 14)

    def test_with_catchup_every_missed_slot_fires_once(self):
        due = utc(2026, 8, 5, 10)
        now = utc(2026, 8, 5, 13, 30)
        slots, _ = scheduling.plan_recurring(self.CRON, timezone='UTC', due_at=due, now=now, catchup=True)
        assert slots == [utc(2026, 8, 5, hour) for hour in (10, 11, 12, 13)]
        assert len(slots) == len(set(slots))

    def test_max_runs_stops_the_schedule(self):
        slots, following = scheduling.plan_recurring(
            self.CRON,
            timezone='UTC',
            due_at=utc(2026, 8, 5, 10),
            now=utc(2026, 8, 5, 13, 30),
            catchup=True,
            max_runs=2,
            runs_count=0,
        )
        assert len(slots) == 2
        assert following is None

    def test_ends_at_stops_the_schedule(self):
        slots, following = scheduling.plan_recurring(
            self.CRON,
            timezone='UTC',
            due_at=utc(2026, 8, 5, 10),
            now=utc(2026, 8, 5, 13, 30),
            catchup=True,
            ends_at=utc(2026, 8, 5, 11, 30),
        )
        assert slots == [utc(2026, 8, 5, 10), utc(2026, 8, 5, 11)]
        assert following is None

    def test_long_outage_is_capped(self):
        """Минутный cron и месячный простой не должны разворачиваться в 43 200 слотов."""
        slots, _ = scheduling.plan_recurring(
            '* * * * *',
            timezone='UTC',
            due_at=utc(2026, 7, 1),
            now=utc(2026, 8, 1),
            catchup=True,
        )
        assert len(slots) <= scheduling.MAX_CATCHUP_SLOTS + 1


class TestRunKey:
    """Формат ключа стабилен: его смена молча выключила бы идемпотентность."""

    def test_format_is_frozen(self):
        assert scheduling.run_key('abc', utc(2026, 8, 5, 9, 0), timezone=MOSCOW) == 'abc:2026-08-05T12:00:00+03:00'

    def test_same_slot_gives_same_key(self):
        slot = utc(2026, 8, 5, 9)
        assert scheduling.run_key('abc', slot) == scheduling.run_key('abc', slot + timedelta(microseconds=500))

    def test_manual_key_collapses_double_click(self):
        moment = utc(2026, 8, 5, 9, 0, 0)
        assert scheduling.manual_run_key('c1', moment) == scheduling.manual_run_key(
            'c1', moment + timedelta(milliseconds=400)
        )

    def test_manual_key_differs_between_campaigns(self):
        moment = utc(2026, 8, 5, 9)
        assert scheduling.manual_run_key('c1', moment) != scheduling.manual_run_key('c2', moment)


def test_invalid_cron_rejected():
    with pytest.raises(scheduling.ScheduleError):
        scheduling.validate_cron('не расписание')


def test_unknown_timezone_rejected():
    with pytest.raises(scheduling.ScheduleError):
        scheduling.resolve_timezone('Mars/Olympus')
