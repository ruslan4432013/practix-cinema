"""Что происходит, когда что-то идёт не так.

Требование задания: «в случае падения любого компонента ничего не должно
потеряться». Проверяются три механизма: dead-letters для того, что починить
нельзя, парковочная очередь с TTL для того, что чинится ожиданием, и outbox,
который не даёт рассылке исчезнуть между коммитом и публикацией.
"""

from conftest import Mailpit, Rabbit, publish_json, publish_raw, wait_until

from practix_notifications.broker import topology
from practix_notifications.campaigns.models import OutboxMessage, ScheduledRun
from practix_notifications.services.launch import launch_now


def test_unparsable_message_goes_to_dead_letters(broker):
    """Битое сообщение повтором не чинится — только в dead-letters.

    Иначе оно возвращалось бы в голову очереди вечно и блокировало всё,
    что стоит за ним.
    """
    publish_raw(broker, routing_key=topology.RK_NOTIFICATION_REQUESTED, body=b'{ not a json at all')
    publish_raw(broker, routing_key=topology.RK_NOTIFICATION_PREPARED, body=b'{ not a json at all')

    depth = Rabbit.wait_for_depth(topology.QUEUE_DEAD_LETTERS, at_least=2)
    assert depth >= 2
    assert Rabbit.queue(topology.QUEUE_BUILD_EMAIL)['messages'] == 0
    assert Rabbit.queue(topology.QUEUE_SEND_EMAIL)['messages'] == 0


def test_message_about_unknown_run_is_dead_lettered(broker):
    """Прогона нет и не появится: повторять бессмысленно.

    Проверяются оба воркера: и формирующий, и отправляющий обязаны отвечать на
    призрачный прогон одинаково.
    """
    for routing_key, event_type in (
        (topology.RK_NOTIFICATION_REQUESTED, 'notification.requested'),
        (topology.RK_NOTIFICATION_PREPARED, 'notification.prepared'),
    ):
        publish_json(
            broker,
            exchange=topology.EXCHANGE_EVENTS,
            routing_key=routing_key,
            payload={
                'schema_version': 1,
                'event_id': 'ghost',
                'occurred_at': '2026-08-05T12:00:00+00:00',
                'type': event_type,
                'run_id': '00000000-0000-0000-0000-000000000000',
                'targets': [],
                'recipients': [],
            },
        )
    Rabbit.wait_for_depth(topology.QUEUE_DEAD_LETTERS, at_least=2)


def test_retry_queue_returns_the_message_after_ttl(broker):
    """Парковочная очередь: полежало, протухло, вернулось в работу.

    Так реализован повтор временных отказов. `nack(requeue=True)` вместо этого
    дал бы горячий цикл: сообщение возвращается мгновенно и жжёт CPU, пока
    почтовый сервер лежит.
    """
    payload = {
        'schema_version': 1,
        'event_id': 'parked',
        'occurred_at': '2026-08-05T12:00:00+00:00',
        'type': 'notification.prepared',
        'run_id': '00000000-0000-0000-0000-000000000000',
        'recipients': [],
    }
    publish_json(
        broker,
        exchange=topology.EXCHANGE_RETRY,
        routing_key=topology.RK_NOTIFICATION_PREPARED,
        payload=payload,
    )

    # Проверяем КОНЕЧНОЕ состояние, а не глубину парковочной очереди по пути:
    # management API RabbitMQ обновляет статистику раз в несколько секунд, а TTL
    # здесь две секунды — сообщение успевает уйти раньше, чем попадёт в отчёт.
    # Оказалось оно в dead-letters — значит вернулось в рабочую точку обмена,
    # было прочитано воркером и отвергнуто им (прогона с таким id не существует).
    # Сами аргументы очереди (TTL и обратный DLX) проверяет test_topology.
    Rabbit.wait_for_depth(topology.QUEUE_DEAD_LETTERS, at_least=1)
    assert Rabbit.queue(topology.QUEUE_RETRY_SEND)['messages'] == 0


def test_outbox_is_drained_and_marked(campaign, subscriber):
    """Сообщение сначала ложится в базу, и только потом уходит в брокер."""
    run = launch_now(campaign)
    assert run is not None
    # Строка появляется в той же транзакции, что и прогон — то есть существует
    # уже сейчас, до того как планировщик вообще узнал о ней.
    assert OutboxMessage.objects.count() == 1

    Mailpit.wait_for(count=1)
    wait_until(
        lambda: not OutboxMessage.objects.filter(published_at__isnull=True).exists(),
        message='Планировщик не слил outbox',
    )
    published = OutboxMessage.objects.get()
    assert published.published_at is not None
    # Аренда не мешает довести строку до конца, а её отметка остаётся видимой:
    # по паре `available_at`/`published_at` в админке читается, сколько заняла
    # публикация и не подбирал ли строку сосед по истёкшей аренде.
    assert published.attempts == 0
    assert published.available_at is not None


def test_delivery_report_is_published(campaign, subscriber):
    """Отчётное событие о судьбе пачки уходит в свою очередь.

    Слушать его никто не обязан — в этом и смысл отчётных событий, — но потерять
    его безадресно нельзя, поэтому очередь объявлена.
    """
    launch_now(campaign)
    Mailpit.wait_for(count=1)
    Rabbit.wait_for_depth(topology.QUEUE_REPORTS, at_least=1)


def test_run_finishes_even_with_empty_audience(campaign):
    """Пустая аудитория — не ошибка и не зависшая навсегда рассылка."""
    launch_now(campaign)
    wait_until(
        lambda: ScheduledRun.objects.filter(campaign=campaign, tasks_created=0, status='published').exists(),
        message='Прогон с пустой аудиторией не завершился',
    )
