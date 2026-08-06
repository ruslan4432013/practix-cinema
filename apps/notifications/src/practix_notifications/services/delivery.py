"""Отправка пачки готовых писем — то, ради чего всё остальное.

## Что этот воркер уже НЕ делает

Он не знает ни шаблона, ни контекста и не ходит за личными данными: письмо
приезжает собранным из очереди ``notifications.send-email``, а собрал его
формирующий воркер (:mod:`practix_notifications.services.building`). Разделение
не косметическое: коннект к SMTP стоит около пяти секунд, поэтому соединение
держится открытым весь срок жизни процесса — и простаивать в нём, ожидая ответа
Auth, было бы прямой потерей пропускной способности.

## Как «хотя бы один раз» превращается в «ровно один раз»

Брокер гарантирует доставку сообщения воркеру «хотя бы один раз»: при разрыве
соединения между отправкой письма и ``ack`` пачка приедет снова. Обеспечить
«ровно один раз» для пользователя может только получатель, и делается это здесь:
задача перечитывается ``FOR UPDATE``, и если она уже ``sent`` — письмо не
отправляется. Теория формулирует то же требование: «клиент должен знать, что
сообщение уже было обработано».

## Что проверяется перед отправкой

1. **Отписка.** Настройки пользователя читаются в момент отправки, а не веера:
   между постановкой в очередь и доставкой человек мог отписаться, и это должно
   сработать.
2. **Актуальность.** Событие с истёкшим ``valid_until`` не отправляется:
   «вышла новая серия, событие создалось, но попало в воркер через два часа» —
   за это время пользователь мог её посмотреть.
3. **Тихие часы** в таймзоне получателя. Не отказ, а отсрочка: получатель
   уезжает в парковочную очередь и вернётся, когда у него наступит утро.

## Почему пачка подтверждается целиком

``ack`` ставится один на сообщение, после того как каждый получатель пришёл в
терминальное состояние. Частично обработанная пачка при переотправке просто
пройдёт мимо уже отправленных — за это отвечает проверка идемпотентности.
"""

import logging
from datetime import UTC, datetime

from django.db import transaction

from practix_notifications.broker import topology
from practix_notifications.broker.consumer import Outcome
from practix_notifications.broker.envelope import (
    PreparedMessage,
    build_delivery_report,
    parse_recipients,
    parse_valid_until,
)
from practix_notifications.broker.publisher import BrokerConnection, PublishFailed
from practix_notifications.campaigns.models import DeliveryTask, ScheduledRun
from practix_notifications.channels import (
    ChannelNotImplemented,
    PermanentDeliveryError,
    RenderedMessage,
    SenderPool,
    TemporaryDeliveryError,
    is_implemented,
)
from practix_notifications.core.config import settings
from practix_notifications.enums import (
    MANDATORY_CATEGORIES,
    AttemptResult,
    Category,
    SkipReason,
    TaskStatus,
)
from practix_notifications.inbox import services as inbox
from practix_notifications.services import journal, quiet_hours
from practix_notifications.services.journal import BatchResult
from practix_notifications.subscribers.models import ChannelOptout

logger = logging.getLogger('notifications.delivery')


def handle_notification_prepared(broker: BrokerConnection, senders: SenderPool, body: dict, attempt: int) -> Outcome:
    """Обработчик очереди ``notifications.send-email``.

    Пул отправителей, а не один отправитель: одна пачка может содержать задачи
    разных каналов, и websocket-уведомление, отданное SMTP-отправителю, ушло бы
    письмом в пустой адрес.
    """
    run = ScheduledRun.objects.filter(pk=body.get('run_id')).select_related('campaign').first()
    if run is None:
        logger.error('Run %s not found, dead-lettering batch', body.get('run_id'))
        return Outcome.DEAD

    recipients = parse_recipients(body)
    valid_until = parse_valid_until(body)
    category = body.get('category') or Category.MARKETING.value

    result = BatchResult()
    for recipient in recipients:
        _deliver_one(
            senders,
            run=run,
            recipient=recipient,
            category=category,
            valid_until=valid_until,
            result=result,
        )
        # Пока smtplib разговаривает с почтовым сервером, pika не обслуживает
        # соединение. Без этого вызова на длинной пачке истечёт heartbeat, брокер
        # закроет канал и переотправит всю пачку заново.
        broker.process_events()

    journal.apply_counters(run, result)

    try:
        # Два вызова, а не один: отложенные тихими часами возвращаются с ТЕМ ЖЕ
        # счётчиком попыток, отказавшие — с увеличенным. Подробности — в ``_park``.
        _park(broker, body, result.deferred, attempt=attempt)
        _park(broker, body, result.retry, attempt=attempt + 1)
    except PublishFailed:
        logger.exception('Could not park deferred recipients')
        return Outcome.RETRY

    _report(broker, run, result)
    return Outcome.ACK


def _deliver_one(
    senders: SenderPool,
    *,
    run: ScheduledRun,
    recipient: PreparedMessage,
    category: str,
    valid_until: datetime | None,
    result: BatchResult,
) -> None:
    started_at = datetime.now(UTC)
    now = started_at

    with transaction.atomic():
        task = (
            DeliveryTask.objects.select_for_update().filter(pk=recipient.task_id).select_related('subscriber').first()
        )
        if task is None:
            logger.warning('Delivery task %s vanished, ignoring recipient', recipient.task_id)
            return
        if task.status in (TaskStatus.SENT.value, TaskStatus.SKIPPED.value):
            # Повторная доставка пачки. Ровно здесь «хотя бы один раз»
            # превращается в «ровно один раз».
            logger.debug('Task %s already terminal (%s), skipping', task.id, task.status)
            return

        skip_reason = _skip_reason(task, category)
        if skip_reason is not None:
            journal.finish(task, TaskStatus.SKIPPED, skip_reason=skip_reason)
            result.skipped += 1
            journal.record(task, AttemptResult.SKIPPED, started_at, error=skip_reason.value)
            return

        if valid_until is not None and now > valid_until:
            journal.finish(task, TaskStatus.SKIPPED, skip_reason=SkipReason.STALE_EVENT)
            result.skipped += 1
            journal.record(task, AttemptResult.SKIPPED, started_at, error='valid_until passed')
            return

        if _is_quiet(run, task.subscriber.timezone, now):
            # Не отказ — отсрочка. Задача возвращается в ожидание, получатель
            # уезжает на парковку и вернётся, когда у него наступит утро.
            task.status = TaskStatus.PENDING.value
            task.save(update_fields=['status', 'updated_at'])
            result.deferred.append(recipient)
            return

        task.attempts += 1
        task.status = TaskStatus.QUEUED.value
        task.save(update_fields=['attempts', 'status', 'updated_at'])
        attempt_no = task.attempts

    # Сообщение приехало собранным. Рендерить здесь нечего — и это единственная
    # функция, которую отправляющий воркер потерял при разделении.
    message = RenderedMessage(
        subject=recipient.subject,
        body=recipient.body,
        is_html=run.is_html,
        headers={
            'X-Notification-Id': str(run.campaign_id),
            'X-Run-Id': str(run.id),
            # По этому заголовку функциональный тест доказывает, что письмо ушло
            # ровно один раз, а не «писем оказалось столько же, сколько ждали».
            'X-Idempotency-Key': recipient.idempotency_key,
            # Ключ, по которому клиент websocket-шлюза склеивает кадр из сокета
            # со строкой ленты: `InboxMessage.task` — OneToOne, значит он
            # стабилен по построению. В письме заголовок безвреден.
            'X-Task-Id': str(recipient.task_id),
        },
    )

    try:
        senders.get(task.channel).send(recipient.address, message)
    except (TemporaryDeliveryError, ChannelNotImplemented, PermanentDeliveryError) as exc:
        _handle_failure(task, recipient, exc, started_at, attempt_no, result)
        return

    with transaction.atomic():
        task = DeliveryTask.objects.select_for_update().get(pk=recipient.task_id)
        journal.finish(task, TaskStatus.SENT)
        journal.record(task, AttemptResult.SENT, started_at, attempt_no=attempt_no)
        # Лента кабинета — в той же транзакции: строка там означает «человека
        # действительно уведомили», и расходиться со статусом задачи она не должна.
        inbox.record(task=task, run=run, message=message, sent_at=task.sent_at or datetime.now(UTC))
    result.sent += 1


def _handle_failure(task, recipient, exc, started_at, attempt_no, result: BatchResult) -> None:
    with transaction.atomic():
        task = DeliveryTask.objects.select_for_update().get(pk=recipient.task_id)
        if isinstance(exc, TemporaryDeliveryError):
            if task.attempts >= settings.NOTIFY_MAX_ATTEMPTS:
                # Бюджет повторов исчерпан. Проверка стоит ЗДЕСЬ, а не в
                # ``broker.consumer``: парковка получателей идёт по ветке ACK,
                # и общий счётчик сообщения там не проверяется вовсе — без этой
                # строки «временный» отказ, который на самом деле постоянный,
                # ходил бы по кругу вечно и никогда не попал бы в dead-letters.
                #
                # Бюджет по ПОЛУЧАТЕЛЮ, а не по сообщению: в одной пачке один
                # адрес может отказывать, а остальные уходить нормально.
                logger.error('Delivery attempts exhausted for task %s after %s tries: %s', task.id, task.attempts, exc)
                journal.finish(task, TaskStatus.FAILED, error=f'attempts exhausted: {exc}')
                result.failed += 1
                journal.record(task, AttemptResult.FAILED, started_at, error=str(exc), attempt_no=attempt_no)
                return
            # Временный отказ: сам получатель уедет в парковочную очередь.
            task.status = TaskStatus.PENDING.value
            task.last_error = str(exc)[:2000]
            task.save(update_fields=['status', 'last_error', 'updated_at'])
            journal.record(task, AttemptResult.RETRY, started_at, error=str(exc), attempt_no=attempt_no)
            result.retry.append(recipient)
            return

        reason = SkipReason.CHANNEL_UNAVAILABLE if isinstance(exc, ChannelNotImplemented) else None
        if reason is not None:
            journal.finish(task, TaskStatus.SKIPPED, skip_reason=reason, error=str(exc))
            result.skipped += 1
            journal.record(task, AttemptResult.SKIPPED, started_at, error=str(exc), attempt_no=attempt_no)
            return

        journal.finish(task, TaskStatus.FAILED, error=str(exc))
        result.failed += 1
        journal.record(task, AttemptResult.FAILED, started_at, error=str(exc), attempt_no=attempt_no)


def _skip_reason(task: DeliveryTask, category: str) -> SkipReason | None:
    subscriber = task.subscriber
    if not subscriber.is_active:
        return SkipReason.INACTIVE
    # ДО проверки адреса: у нереализованного канала адреса нет по построению
    # (формирующий воркер оставляет его пустым для всего, кроме email и
    # websocket), и `no_address` называл бы симптом вместо причины — менеджер
    # увидел бы «нет адреса» там, где на самом деле «нет канала».
    # UnimplementedSender остаётся второй линией обороны.
    if not is_implemented(task.channel):
        return SkipReason.CHANNEL_UNAVAILABLE
    if not task.address:
        return SkipReason.NO_ADDRESS
    if _opted_out(str(subscriber.id), task.channel, category):
        return SkipReason.OPTED_OUT
    return None


def _opted_out(subscriber_id: str, channel: str, category: str) -> bool:
    """Транзакционные письма не отключаются: чек об оплате обязан дойти."""
    if category in {member.value for member in MANDATORY_CATEGORIES}:
        return False
    return ChannelOptout.objects.filter(
        subscriber_id=subscriber_id, channel=channel, category__in=['', category]
    ).exists()


def _is_quiet(run: ScheduledRun, timezone: str, now: datetime) -> bool:
    """Тихие часы считаются по таймзоне из АКТУАЛЬНОЙ строки подписчика.

    Она уже прочитана под ``select_for_update`` вместе с задачей, поэтому вести
    таймзону через очередь незачем — а везти значило бы решать судьбу письма по
    данным, устаревшим на всё время ожидания в очереди.
    """
    if not settings.NOTIFY_QUIET_HOURS_ENABLED or not run.campaign.respect_quiet_hours:
        return False
    return quiet_hours.is_quiet(
        now,
        timezone=timezone or settings.NOTIFY_DEFAULT_TIMEZONE,
        start=quiet_hours.parse_time(settings.NOTIFY_QUIET_HOURS_START),
        end=quiet_hours.parse_time(settings.NOTIFY_QUIET_HOURS_END),
    )


def _park(broker: BrokerConnection, body: dict, recipients: list[PreparedMessage], *, attempt: int) -> None:
    """Вернуть получателей в ОЧЕРЕДЬ ОТПРАВКИ, а не сборки.

    Ключ маршрутизации указан явно: письма уже собраны, и отскок не должен
    гонять их через Auth заново. При TTL в 30 секунд восьмичасовое ночное окно
    превратилось бы почти в тысячу лишних запросов на каждого получателя.

    ``attempt`` передаётся вызывающим, и в этом весь смысл разделения:

    * отложенным тихими часами он приходит НЕИЗМЕНЁННЫМ — отсрочка до утра
      обязана пережить сколько угодно отскоков;
    * отказавшим — увеличенным на единицу, чтобы счётчик в заголовке не врал.

    Бюджет отказов при этом считается не здесь, а по ``DeliveryTask.attempts``
    в ``_handle_failure``: парковка идёт по ветке ACK, и проверка в
    ``broker.consumer`` до неё не доходит.
    """
    if not recipients:
        return
    parked = {**body, 'recipients': [item.as_dict() for item in recipients]}
    broker.republish_to_retry(
        body=parked,
        headers={},
        attempt=attempt,
        routing_key=topology.RK_NOTIFICATION_PREPARED,
    )


def _report(broker: BrokerConnection, run: ScheduledRun, result: BatchResult) -> None:
    """Отчётное событие о судьбе пачки.

    Слушать его никто не обязан — в этом и смысл отчётных событий: сервис
    сообщает о результате своей работы, а кто на него подпишется, он не знает.
    """
    body = build_delivery_report(
        run_id=str(run.id),
        campaign_id=str(run.campaign_id),
        delivered=result.sent,
        failed=result.failed,
        skipped=result.skipped,
    )
    routing_key = topology.RK_NOTIFICATION_FAILED if result.failed else topology.RK_NOTIFICATION_DELIVERED
    try:
        broker.publish(
            exchange=topology.EXCHANGE_EVENTS,
            routing_key=routing_key,
            body=body,
            # Срок жизни на сообщении: очередь отчётов никто не разгребает, и без
            # него она растёт всё время работы сервиса. Аргументом очереди это
            # уже не задать — она объявлена на всех работающих стендах, а
            # переобъявление с новыми аргументами даёт 406 и краш-цикл воркеров.
            expiration_ms=settings.NOTIFY_REPORT_TTL_MS,
        )
    except PublishFailed:
        # Отчёт — не доставка. Его потеря не повод переотправлять письма.
        logger.warning('Delivery report not published', exc_info=True)
