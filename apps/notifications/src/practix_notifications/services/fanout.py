"""Веер: разворачивание рассылки в получателей.

Здесь проходит граница ответственности за данные, и она сдвинута по сравнению с
первой версией сервиса:

* ДО очереди (в этом модуле) определяется только КОГО уведомить: аудитория,
  задачи доставки и их ключи идемпотентности. В сообщение уезжают ТОЛЬКО
  идентификаторы;
* В ФОРМИРУЮЩЕМ воркере (``services.building``) появляется личность: имя,
  фамилия и адрес запрашиваются у Auth, по ним собирается текст письма;
* В ОТПРАВЛЯЮЩЕМ воркере (``services.delivery``) — свежесть события, отписки,
  тихие часы и собственно SMTP.

Раньше веер клал в очередь адрес, таймзону и ник одним запросом к своей базе.
Так дешевле, но личные данные тогда застывают в очереди на всё время ожидания, а
имени в витрине подписчиков нет вовсе. Теория («Отправка уведомления») отдаёт
сбор данных именно воркеру: «Лучший вариант — собирать данные воркером в
неспешном режиме». Цена решения — готовые тела писем в очереди отправки, поэтому
там пачки заметно меньше (``NOTIFY_BUILD_MESSAGE_BATCH``).

## Почему пачками

Рассылка на 100 000 человек одним сообщением — это сообщение, которое невозможно
переотправить частично. По сообщению на человека — это 100 000 публикаций и
забитый брокер. Пачка (``NOTIFY_FANOUT_MESSAGE_BATCH``) — компромисс, который
теория и предлагает обдумать: «По одному событию для каждого пользователя или
одно событие, охватывающее всех? А может, пачками по 1000?»
"""

import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from django.db import transaction

from practix_notifications.broker import topology
from practix_notifications.broker.consumer import Outcome
from practix_notifications.broker.envelope import TargetRef, build_notification_requested
from practix_notifications.broker.publisher import BrokerConnection, PublishFailed
from practix_notifications.campaigns.models import Campaign, DeliveryTask, ScheduledRun
from practix_notifications.core.config import settings
from practix_notifications.enums import CampaignStatus, Channel, RunStatus, SegmentKind, TaskStatus
from practix_notifications.services.idempotency import delivery_key
from practix_notifications.subscribers.models import SEGMENT_FILTER_FIELDS, Segment, Subscriber

logger = logging.getLogger('notifications.fanout')


def handle_campaign_launched(broker: BrokerConnection, body: dict, attempt: int) -> Outcome:
    """Обработчик очереди ``notifications.fan-out-campaign``."""
    run_id = body.get('run_id')
    run = (
        ScheduledRun.objects.filter(pk=run_id)
        .select_related('campaign', 'campaign__template', 'audience_subscriber')
        .first()
    )
    if run is None:
        # Прогона нет и не появится: чинить повтором нечего.
        logger.error('Run %s not found, dead-lettering', run_id)
        return Outcome.DEAD

    if run.status == RunStatus.PUBLISHED.value:
        # Повторная доставка события. Задачи уже созданы, пачки разосланы.
        logger.info('Run %s already fanned out, skipping', run_id)
        return Outcome.ACK

    try:
        created = _fan_out(broker, run)
    except PublishFailed as exc:
        logger.warning('Broker unavailable during fan-out of %s: %s', run_id, exc)
        return Outcome.RETRY

    logger.info('Fan-out done', extra={'run_id': str(run.id), 'recipients': created})
    return Outcome.ACK


def _fan_out(broker: BrokerConnection, run: ScheduledRun) -> int:
    campaign = run.campaign
    template = campaign.template
    # Канал события побеждает канал рассылки: свободный формат позволяет
    # вызывающему выбрать его явно, а триггерная рассылка одна на все каналы.
    channel = run.channel_override or campaign.channel
    now = datetime.now(UTC)
    valid_until = now + timedelta(hours=settings.NOTIFY_MESSAGE_TTL_HOURS)

    ScheduledRun.objects.filter(pk=run.pk).update(status=RunStatus.FANNING_OUT.value, started_at=now)
    if not run.is_targeted:
        Campaign.objects.filter(pk=campaign.pk).update(status=CampaignStatus.RUNNING.value)

    total = 0
    pending: list[TargetRef] = []
    for chunk in _iter_subscribers(run, channel):
        tasks = _create_tasks(run, campaign, channel, chunk)
        total += len(tasks)
        for task in tasks:
            pending.append(_to_target(task))
            if len(pending) >= settings.NOTIFY_FANOUT_MESSAGE_BATCH:
                _publish_batch(broker, run, campaign, template, channel, valid_until, pending)
                pending = []
    if pending:
        _publish_batch(broker, run, campaign, template, channel, valid_until, pending)

    ScheduledRun.objects.filter(pk=run.pk).update(status=RunStatus.PUBLISHED.value, tasks_created=total)
    if not run.is_targeted:
        Campaign.objects.filter(pk=campaign.pk).update(recipients_total=total)
        if total == 0:
            # Пустая аудитория — не ошибка, но и «отправляется» она не будет.
            Campaign.objects.filter(pk=campaign.pk).update(status=CampaignStatus.DONE.value)
    return total


def _iter_subscribers(run: ScheduledRun, channel: str) -> Iterator[list[Subscriber]]:
    """Аудитория прогона пачками, курсором по ``id``.

    Курсор, а не ``LIMIT/OFFSET``: на сотнях тысяч строк OFFSET заставляет базу
    каждый раз перечитывать всё пропущенное, и последняя пачка обходится дороже
    первой в разы.

    Канал нужен здесь ровно для одного: отсев безадресных зависит от него. Для
    рассылки в websocket отсутствие почты не значит ничего — адресом там служит
    идентификатор подписчика.
    """
    if run.is_targeted:
        # Адресный прогон по событию: получатель ровно один и он уже известен.
        # Фильтра по активности и непустому email здесь НЕТ намеренно: для
        # массовой рассылки безадресный подписчик — шум, а для письма конкретному
        # человеку это факт, который менеджер обязан увидеть в журнале как
        # `no_address` или `inactive`. Ветки «получателя не существует» тоже нет:
        # внешний ключ с CASCADE уносит прогон вместе с подписчиком.
        yield [run.audience_subscriber]
        return

    queryset = Subscriber.objects.filter(is_active=True).order_by('id')
    if channel == Channel.EMAIL.value:
        queryset = queryset.exclude(email='')
    segment = run.campaign.segment
    if segment is not None:
        queryset = _apply_segment(queryset, segment)

    last_id = None
    while True:
        page = queryset if last_id is None else queryset.filter(id__gt=last_id)
        chunk = list(page[: settings.NOTIFY_FANOUT_DB_BATCH])
        if not chunk:
            return
        yield chunk
        last_id = chunk[-1].id


def _apply_segment(queryset, segment: Segment):
    if segment.kind == SegmentKind.STATIC.value:
        return queryset.filter(segment_memberships__segment=segment)
    if segment.kind == SegmentKind.FILTER.value:
        # Условие описывается менеджером в JSON и применяется к БЕЛОМУ СПИСКУ
        # полей (`SEGMENT_FILTER_FIELDS`): пробросить его прямо в filter(**)
        # значило бы дать доступ ко всей схеме через связи
        # (`subscriber__tasks__campaign__...`).
        criteria = segment.filter or {}
        unknown = sorted(set(criteria) - SEGMENT_FILTER_FIELDS)
        if unknown:
            # Молча выбрасывать неизвестные ключи НЕЛЬЗЯ. `{"locale__in": [...]}`
            # — первая же опечатка, которую напишет менеджер, — сократилась бы до
            # пустого условия, то есть до `filter()`, то есть до рассылки на ВСЮ
            # активную базу вместо горстки людей. Пустая аудитория — заметная
            # ошибка, лишняя рассылка на сто тысяч человек — неисправимая.
            logger.error('Segment %s has unsupported filter keys %s, treating audience as empty', segment.code, unknown)
            return queryset.none()
        return queryset.filter(**criteria)
    return queryset


def _create_tasks(run: ScheduledRun, campaign: Campaign, channel: str, chunk: list[Subscriber]) -> list[DeliveryTask]:
    """Создать задачи доставки идемпотентно и вернуть РЕАЛЬНЫЕ строки из базы.

    ``ignore_conflicts=True`` пропускает уже существующие задачи, но объекты в
    памяти сохраняют свежесгенерированные UUID, которых в базе нет. Поэтому после
    вставки строки перечитываются по ключу идемпотентности: иначе при повторной
    доставке события веера в очередь уехали бы ссылки на несуществующие задачи.
    """
    keys = []
    candidates = []
    for subscriber in chunk:
        key = delivery_key(str(run.id), str(subscriber.id), channel)
        keys.append(key)
        candidates.append(
            DeliveryTask(
                run=run,
                campaign=campaign,
                subscriber=subscriber,
                channel=channel,
                # Адрес здесь НЕ проставляется: его пропишет формирующий воркер,
                # получив от Auth актуальный. Локальная копия годится для отбора
                # аудитории, но не для того, чтобы отправить по ней письмо.
                idempotency_key=key,
                status=TaskStatus.PENDING.value,
            )
        )

    with transaction.atomic():
        DeliveryTask.objects.bulk_create(candidates, ignore_conflicts=True)
        tasks = list(DeliveryTask.objects.filter(idempotency_key__in=keys).select_related('subscriber'))
        DeliveryTask.objects.filter(idempotency_key__in=keys, status=TaskStatus.PENDING.value).update(
            status=TaskStatus.QUEUED.value, queued_at=datetime.now(UTC)
        )
    return tasks


def _to_target(task: DeliveryTask) -> TargetRef:
    """Только идентификаторы: ни адреса, ни имени, ни таймзоны.

    Требование задания в одной функции. Личность добавит формирующий воркер,
    сходив за ней в Auth.
    """
    return TargetRef(
        task_id=str(task.id),
        idempotency_key=task.idempotency_key,
        subscriber_id=str(task.subscriber_id),
    )


def _publish_batch(
    broker: BrokerConnection,
    run: ScheduledRun,
    campaign: Campaign,
    template,
    channel: str,
    valid_until: datetime,
    targets: list[TargetRef],
) -> None:
    broker.publish(
        exchange=topology.EXCHANGE_EVENTS,
        routing_key=topology.RK_NOTIFICATION_REQUESTED,
        body=build_notification_requested(
            run_id=str(run.id),
            campaign_id=str(campaign.id),
            channel=channel,
            category=campaign.category,
            content_id=campaign.content_id,
            template_code=template.code,
            template_revision=run.template_revision,
            # Порядок победы: переменные получателя (в building) > переменные
            # события > контекст рассылки. Событие знает про конкретный фильм,
            # рассылка — только про себя.
            context={
                'campaign_title': campaign.name,
                **(campaign.context or {}),
                **(run.context_overrides or {}),
                # Куда увести после подтверждения email. Приоритет: поле
                # кампании > ключ `redirect_url`, вписанный в её JSON-контекст >
                # NOTIFY_CONFIRM_REDIRECT_URL (подставляется уже в building).
                # Поле сильнее ключа намеренно: оно валидируется формой админки,
                # а ключ в JSON молчит на любой опечатке.
                **({'redirect_url': campaign.confirm_redirect_url} if campaign.confirm_redirect_url else {}),
            },
            valid_until=valid_until,
            targets=targets,
        ),
    )
    # Ручка на случай, когда пиковый RPS на запись начнёт мешать соседним
    # очередям того же сервера. По умолчанию 0 — тормозить без нужды незачем.
    if settings.NOTIFY_PUBLISH_SLEEP:
        time.sleep(settings.NOTIFY_PUBLISH_SLEEP)
