"""Журнал доставки: что случилось с пачкой и как это отражается в базе.

Общий код ДВУХ воркеров. Формирующий закрывает задачи, до отправки которых дело
не дойдёт (человека нет в Auth, у него нет адреса, шаблон не собрался),
отправляющий — всё остальное. Счётчики кампании и закрытие прогона обязаны
считаться одинаково с обеих сторон: разъехавшись, они сначала покажут менеджеру
неверные цифры, а потом навсегда оставят рассылку в статусе «идёт».

Отдельный модуль, а не два похожих набора приватных функций, ещё и потому, что
дублирование в репозитории — жёсткий гейт (jscpd).
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from django.db.models import F

from practix_core.context import get_request_id
from practix_notifications.campaigns.models import Campaign, DeliveryAttempt, DeliveryTask, ScheduledRun
from practix_notifications.enums import AttemptResult, CampaignStatus, RunStatus, SkipReason, TaskStatus

logger = logging.getLogger('notifications.journal')


class BatchResult:
    """Что случилось с пачкой. Отдельный объект — чтобы обработчик остался читаемым."""

    def __init__(self) -> None:
        self.sent = 0
        self.failed = 0
        self.skipped = 0
        #: Получатели, которых нужно вернуть в очередь после ВРЕМЕННОГО ОТКАЗА
        #: (почтовый сервер не ответил). Возврат стоит одной попытки из бюджета.
        #: Тип зависит от воркера, поэтому список нетипизирован.
        self.retry: list[Any] = []
        #: Получатели, ОТЛОЖЕННЫЕ до утра тихими часами. Отдельный список, а не
        #: тот же самый: отсрочка и отказ ждут разного и стоят разного. Ночное
        #: окно в восемь часов при TTL парковки в тридцать секунд — это около
        #: тысячи отскоков, и, разделяй их бюджет, письмо не пережило бы ни одной
        #: ночи. См. ``delivery._park``.
        self.deferred: list[Any] = []


def finish(task: DeliveryTask, status: TaskStatus, *, skip_reason: SkipReason | None = None, error: str = '') -> None:
    """Перевести задачу в терминальное состояние."""
    task.status = status.value
    if skip_reason is not None:
        task.skip_reason = skip_reason.value
    if error:
        task.last_error = error[:2000]
    if status is TaskStatus.SENT:
        task.sent_at = datetime.now(UTC)
    task.save(update_fields=['status', 'skip_reason', 'last_error', 'sent_at', 'updated_at'])


def record(
    task: DeliveryTask, result: AttemptResult, started_at: datetime, *, error: str = '', attempt_no: int = 0
) -> None:
    """Строка в журнале попыток — то, по чему потом разбирают инцидент."""
    DeliveryAttempt.objects.create(
        task=task,
        attempt_no=attempt_no or task.attempts,
        result=result.value,
        error=error[:2000],
        request_id=get_request_id() or '',
        started_at=started_at,
        finished_at=datetime.now(UTC),
    )


def purge_attempts(*, older_than_days: int, now: datetime | None = None) -> int:
    """Удалить старые строки журнала попыток. Возвращает число удалённых.

    Журнал растёт вместе с задачами доставки — по строке на каждую попытку
    каждого получателя каждой рассылки, — и, в отличие от ленты кабинета, срока
    хранения у него не было. Разбирают по нему инцидент недельной давности, а не
    прошлогодний, поэтому ``NOTIFY_ATTEMPTS_RETENTION_DAYS`` заметно меньше, чем
    хотелось бы «на всякий случай»: счётчики рассылки и терминальный статус
    задачи остаются в ``delivery_task`` навсегда, здесь удаляется только история
    попыток.
    """
    threshold = (now or datetime.now(UTC)) - timedelta(days=older_than_days)
    deleted, _ = DeliveryAttempt.objects.filter(finished_at__lt=threshold).delete()
    if deleted:
        logger.info('Purged %d delivery attempts older than %s', deleted, threshold.isoformat())
    return deleted


def apply_counters(run: ScheduledRun, result: BatchResult) -> None:
    """Обновить счётчики кампании и закрыть прогон, если незавершённых задач нет.

    Вызывают ОБА воркера, каждый за свои исходы. Иначе пачка, целиком отсеянная
    на этапе сборки (никого из этих людей больше нет в Auth), не закрыла бы
    прогон, и кампания навсегда осталась бы в статусе «идёт».
    """
    Campaign.objects.filter(pk=run.campaign_id).update(
        sent_count=F('sent_count') + result.sent,
        failed_count=F('failed_count') + result.failed,
        skipped_count=F('skipped_count') + result.skipped,
    )
    if result.sent:
        Campaign.objects.filter(pk=run.campaign_id).update(last_notification_sent_at=datetime.now(UTC))

    unfinished = DeliveryTask.objects.filter(
        run=run, status__in=[TaskStatus.PENDING.value, TaskStatus.QUEUED.value]
    ).exists()
    if not unfinished:
        ScheduledRun.objects.filter(pk=run.pk).update(status=RunStatus.PUBLISHED.value, finished_at=datetime.now(UTC))
        if not run.is_targeted:
            # Адресный прогон рассылку не закрывает: письмо одному человеку по
            # событию может завершиться ровно в тот момент, когда та же
            # триггерная рассылка разворачивается на всех, — и объявило бы её
            # завершённой посреди веера.
            Campaign.objects.filter(pk=run.campaign_id, status=CampaignStatus.RUNNING.value).update(
                status=CampaignStatus.DONE.value
            )
