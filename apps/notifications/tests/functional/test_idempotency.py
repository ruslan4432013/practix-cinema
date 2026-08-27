"""«Хотя бы один раз» от брокера — «ровно один раз» для пользователя.

Повторная доставка сообщения — штатное поведение AMQP, а не сбой: при разрыве
соединения между отправкой письма и ``ack`` пачка приедет снова. Здесь
проверяется, что второе письмо от этого не появляется.

Считаем не «сколько писем пришло», а «сколько РАЗНЫХ ключей идемпотентности» —
иначе тест зелёный и при одном письме, и при двух, если второе не успело дойти.
"""

from conftest import Mailpit, publish_json

from practix_notifications.broker import topology
from practix_notifications.campaigns.models import DeliveryTask, ScheduledRun
from practix_notifications.enums import TaskStatus
from practix_notifications.services.launch import launch_now


def _prepared_body(run: ScheduledRun, task: DeliveryTask, subscriber) -> dict:
    """Пачка ГОТОВЫХ писем — то, что видит отправляющий воркер."""
    return {
        'schema_version': 1,
        'event_id': 'test-event',
        'occurred_at': '2026-08-05T12:00:00+00:00',
        'type': 'notification.prepared',
        'run_id': str(run.id),
        'notification_id': str(run.campaign_id),
        'channel': 'email',
        'category': 'marketing',
        'content_id': '',
        'recipients': [
            {
                'task_id': str(task.id),
                'idempotency_key': task.idempotency_key,
                'subscriber_id': str(subscriber.id),
                'address': task.address,
                'subject': 'Привет, Иван',
                'body': '<p>Повтор</p>',
            }
        ],
    }


def _build_body(run: ScheduledRun, task: DeliveryTask, subscriber) -> dict:
    """Пачка ИДЕНТИФИКАТОРОВ — то, что видит формирующий воркер."""
    return {
        'schema_version': 1,
        'event_id': 'test-build-event',
        'occurred_at': '2026-08-05T12:00:00+00:00',
        'type': 'notification.requested',
        'run_id': str(run.id),
        'notification_id': str(run.campaign_id),
        'channel': 'email',
        'category': 'marketing',
        'content_id': '',
        'template': {'code': 'functional', 'revision': 1},
        'context': {'film_title': 'Дюна'},
        'targets': [
            {
                'task_id': str(task.id),
                'idempotency_key': task.idempotency_key,
                'subscriber_id': str(subscriber.id),
            }
        ],
    }


def test_redelivered_batch_does_not_send_twice(broker, campaign, subscriber):
    launch_now(campaign)
    first = Mailpit.wait_for(count=1)
    task = DeliveryTask.objects.get()
    run = ScheduledRun.objects.get()

    # Ровно то, что делает брокер после разрыва: та же пачка ещё раз.
    publish_json(
        broker,
        exchange=topology.EXCHANGE_EVENTS,
        routing_key=topology.RK_NOTIFICATION_PREPARED,
        payload=_prepared_body(run, task, subscriber),
    )

    # Ждём, пока воркер точно успел разобрать повтор, и только потом считаем.
    Mailpit.assert_stable(count=1, seconds=5)
    messages = Mailpit.messages()
    assert len(messages) == len(first) == 1

    keys = {tuple(Mailpit.headers(item['ID']).get('X-Idempotency-Key', [])) for item in messages}
    assert keys == {(task.idempotency_key,)}


def test_task_stays_sent_after_redelivery(broker, campaign, subscriber):
    launch_now(campaign)
    Mailpit.wait_for(count=1)
    task = DeliveryTask.objects.get()
    run = ScheduledRun.objects.get()
    attempts_before = task.delivery_attempts.count()

    publish_json(
        broker,
        exchange=topology.EXCHANGE_EVENTS,
        routing_key=topology.RK_NOTIFICATION_PREPARED,
        payload=_prepared_body(run, task, subscriber),
    )
    Mailpit.assert_stable(count=1, seconds=5)

    task.refresh_from_db()
    assert task.status == TaskStatus.SENT.value
    # Повтор не порождает новой попытки: воркер выходит до отправки.
    assert task.delivery_attempts.count() == attempts_before


def test_redelivered_build_batch_does_not_send_twice(broker, campaign, subscriber):
    """Идемпотентность держится на ОБОИХ шагах, а не только на отправке.

    Повторная пачка сборки не должна ни собрать второе письмо, ни сходить за
    личными данными: задача уже терминальна, и формирующий воркер выходит до
    обращения к Auth.
    """
    launch_now(campaign)
    Mailpit.wait_for(count=1)
    task = DeliveryTask.objects.get()
    run = ScheduledRun.objects.get()

    publish_json(
        broker,
        exchange=topology.EXCHANGE_EVENTS,
        routing_key=topology.RK_NOTIFICATION_REQUESTED,
        payload=_build_body(run, task, subscriber),
    )

    Mailpit.assert_stable(count=1, seconds=5)
    task.refresh_from_db()
    assert task.status == TaskStatus.SENT.value


def test_repeated_fanout_event_does_not_duplicate_tasks(broker, campaign, subscriber):
    """Повторная доставка события веера не создаёт вторых задач доставки."""
    launch_now(campaign)
    Mailpit.wait_for(count=1)
    run = ScheduledRun.objects.get()

    publish_json(
        broker,
        exchange=topology.EXCHANGE_EVENTS,
        routing_key=topology.RK_CAMPAIGN_LAUNCHED,
        payload={
            'schema_version': 1,
            'event_id': 'test-fanout',
            'occurred_at': '2026-08-05T12:00:00+00:00',
            'type': 'campaign.launched',
            'run_id': str(run.id),
            'notification_id': str(run.campaign_id),
        },
    )

    Mailpit.assert_stable(count=1, seconds=5)
    assert DeliveryTask.objects.count() == 1
