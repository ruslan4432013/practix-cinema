"""Отложенные и повторяющиеся рассылки — два «плюсом» пункта задания.

Проверяется настоящий планировщик в соседнем контейнере: тест только кладёт
расписание в базу, а тикает по нему отдельный процесс.
"""

from datetime import UTC, datetime, timedelta

from conftest import Mailpit, wait_until

from practix_notifications.campaigns.models import CampaignSchedule, ScheduledRun
from practix_notifications.enums import ScheduleKind


def test_deferred_campaign_waits_and_then_sends(campaign, subscriber):
    """«Отправить через N часов»: до срока писем нет, после срока письмо есть."""
    run_at = datetime.now(UTC) + timedelta(seconds=5)
    CampaignSchedule.objects.create(
        campaign=campaign,
        kind=ScheduleKind.DEFERRED.value,
        run_at=run_at,
        next_run_at=run_at,
        timezone='UTC',
    )

    # Пока срок не наступил, планировщик обязан молчать.
    Mailpit.assert_stable(count=0, seconds=3)
    Mailpit.wait_for(count=1)

    run = ScheduledRun.objects.get(campaign=campaign)
    assert run.planned_for >= run_at - timedelta(seconds=1)


def test_deferred_schedule_does_not_fire_twice(campaign, subscriber):
    run_at = datetime.now(UTC) - timedelta(seconds=1)
    CampaignSchedule.objects.create(
        campaign=campaign,
        kind=ScheduleKind.DEFERRED.value,
        run_at=run_at,
        next_run_at=run_at,
        timezone='UTC',
    )

    Mailpit.wait_for(count=1)
    Mailpit.assert_stable(count=1, seconds=5)

    schedule = CampaignSchedule.objects.get(campaign=campaign)
    assert schedule.next_run_at is None, 'Отложенная рассылка обязана заснуть навсегда'
    assert schedule.is_enabled is False
    assert ScheduledRun.objects.filter(campaign=campaign).count() == 1


def test_recurring_campaign_fires_repeatedly(campaign, subscriber):
    """«Каждую пятницу» проверяется тем же механизмом, но с шагом в секунды."""
    first = datetime.now(UTC)
    CampaignSchedule.objects.create(
        campaign=campaign,
        kind=ScheduleKind.RECURRING.value,
        # Шестое поле croniter — СЕКУНДЫ, и стоит оно ПОСЛЕ дня недели:
        # «* * * * * */5» это «каждые 5 секунд», а «*/5 * * * * *» — совсем
        # другое расписание (каждая пятая минута). Недельный cron проверять
        # некогда, а логика пересчёта next_run_at одна и та же.
        cron_expression='* * * * * */5',
        timezone='UTC',
        next_run_at=first,
    )

    Mailpit.wait_for(count=2, timeout=40)

    runs = list(ScheduledRun.objects.filter(campaign=campaign).order_by('planned_for'))
    assert len(runs) >= 2
    # Разные ключи запуска — значит это действительно два разных слота, а не
    # один, обработанный дважды.
    assert len({run.run_key for run in runs}) == len(runs)

    schedule = CampaignSchedule.objects.get(campaign=campaign)
    assert schedule.runs_count >= 2
    assert schedule.next_run_at is not None


def test_missed_slots_do_not_pile_up_after_downtime(campaign, subscriber):
    """Требование: после простоя генератора не должны дублироваться события.

    Расписание с ежеминутным cron и next_run_at десятиминутной давности — это
    ровно десять пропущенных слотов. Без политики догона сработать обязан один.
    """
    now = datetime.now(UTC)
    CampaignSchedule.objects.create(
        campaign=campaign,
        kind=ScheduleKind.RECURRING.value,
        cron_expression='* * * * *',
        timezone='UTC',
        catchup=False,
        next_run_at=now - timedelta(minutes=10),
        # Расписание истекает «сейчас»: очередной штатный слот наступил бы через
        # минуту и дал бы второе письмо — совершенно законное, но превращающее
        # проверку в гонку с часами.
        ends_at=now,
    )

    Mailpit.wait_for(count=1)
    Mailpit.assert_stable(count=1, seconds=6)
    assert ScheduledRun.objects.filter(campaign=campaign).count() == 1
    assert CampaignSchedule.objects.get(campaign=campaign).next_run_at is None


def test_disabled_schedule_stays_silent(campaign, subscriber):
    CampaignSchedule.objects.create(
        campaign=campaign,
        kind=ScheduleKind.DEFERRED.value,
        run_at=datetime.now(UTC) - timedelta(seconds=1),
        next_run_at=datetime.now(UTC) - timedelta(seconds=1),
        is_enabled=False,
        timezone='UTC',
    )
    Mailpit.assert_stable(count=0, seconds=5)
    assert not ScheduledRun.objects.filter(campaign=campaign).exists()


def test_max_runs_stops_the_schedule(campaign, subscriber):
    CampaignSchedule.objects.create(
        campaign=campaign,
        kind=ScheduleKind.RECURRING.value,
        cron_expression='* * * * * */2',
        timezone='UTC',
        max_runs=1,
        next_run_at=datetime.now(UTC),
    )

    Mailpit.wait_for(count=1)
    wait_until(
        lambda: CampaignSchedule.objects.get(campaign=campaign).next_run_at is None,
        message='Расписание не остановилось после исчерпания лимита запусков',
    )
    Mailpit.assert_stable(count=1, seconds=5)
