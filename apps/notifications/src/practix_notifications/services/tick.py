"""Тик планировщика: кто должен сработать прямо сейчас.

Единственный запрос выборки — по индексированному ``next_run_at``. Не «пройти все
расписания и посчитать», а «взять те, чьё время пришло»: при тысяче расписаний
разница между этими двумя формулировками — это разница между индексом и
последовательным сканом каждые тридцать секунд.

``FOR UPDATE SKIP LOCKED`` — то, что позволяет держать больше одной реплики
планировщика: вторая проходит мимо строк, занятых первой, вместо ожидания на
блокировке. Вторая линия защиты от дублей — уникальный ``run_key``: даже если обе
реплики каким-то образом возьмут один слот, запуск создастся один.
"""

import logging
from datetime import UTC, datetime

from django.db import transaction

from practix_notifications.campaigns.models import CampaignSchedule
from practix_notifications.core.config import settings
from practix_notifications.enums import CampaignStatus, ScheduleKind
from practix_notifications.services import scheduling
from practix_notifications.services.launch import LaunchError, launch

logger = logging.getLogger('notifications.tick')


def tick(now: datetime | None = None) -> int:
    """Запустить всё, чьё время пришло. Возвращает число созданных прогонов."""
    moment = now or datetime.now(UTC)
    fired = 0
    for _ in range(settings.NOTIFY_SCHEDULE_LOCK_BATCH):
        with transaction.atomic():
            schedule = (
                CampaignSchedule.objects.select_for_update(skip_locked=True)
                .filter(is_enabled=True, next_run_at__isnull=False, next_run_at__lte=moment)
                .select_related('campaign', 'campaign__template')
                .order_by('next_run_at')
                .first()
            )
            if schedule is None:
                break
            fired += _fire(schedule, moment)
    return fired


def _fire(schedule: CampaignSchedule, now: datetime) -> int:
    slots, following = _plan(schedule, now)

    created = 0
    for slot in slots:
        key = scheduling.run_key(str(schedule.id), slot, timezone=schedule.timezone)
        try:
            run = launch(schedule.campaign, run_key=key, planned_for=slot, schedule=schedule)
        except LaunchError as exc:
            # Выключенный шаблон — ошибка настройки, а не сбой. Расписание
            # останавливается, чтобы не сыпать одной и той же ошибкой каждый тик.
            logger.error('Schedule %s stopped: %s', schedule.id, exc)
            CampaignSchedule.objects.filter(pk=schedule.pk).update(is_enabled=False, next_run_at=None)
            schedule.campaign.status = CampaignStatus.FAILED.value
            schedule.campaign.save(update_fields=['status'])
            return created
        if run is not None:
            created += 1

    CampaignSchedule.objects.filter(pk=schedule.pk).update(
        next_run_at=following,
        last_run_at=now,
        runs_count=schedule.runs_count + created,
        # Расписание без следующего срабатывания больше не проснётся. Гасим флаг
        # явно, чтобы это было видно в админке, а не выводилось из пустого поля.
        is_enabled=following is not None,
    )
    return created


def _plan(schedule: CampaignSchedule, now: datetime) -> tuple[list[datetime], datetime | None]:
    due = schedule.next_run_at
    if schedule.kind == ScheduleKind.RECURRING.value and schedule.cron_expression:
        try:
            return scheduling.plan_recurring(
                schedule.cron_expression,
                timezone=schedule.timezone,
                due_at=due,
                now=now,
                catchup=schedule.catchup,
                ends_at=schedule.ends_at,
                max_runs=schedule.max_runs,
                runs_count=schedule.runs_count,
            )
        except scheduling.ScheduleError:
            logger.exception('Broken cron in schedule %s, disabling', schedule.id)
            return [], None
    # Отложенная и немедленная рассылки срабатывают один раз и засыпают навсегда.
    return [due], None
