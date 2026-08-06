"""Топология брокера: то, что обещано в коде, объявлено и на сервере.

Проверка дешёвая и ловит целый класс молчаливых поломок: очередь без
``durable`` теряется при перезагрузке сервера, очередь без
``x-dead-letter-exchange`` тихо выбрасывает неразбираемые сообщения, а
парковочная очередь без TTL превращается в яму, из которой ничего не
возвращается.
"""

from conftest import Mailpit, Rabbit

from practix_notifications.broker import topology
from practix_notifications.core.config import settings
from practix_notifications.services.launch import launch_now

EXPECTED_QUEUES = (
    topology.QUEUE_FANOUT,
    topology.QUEUE_BUILD_EMAIL,
    topology.QUEUE_SEND_EMAIL,
    topology.QUEUE_RETRY_BUILD,
    topology.QUEUE_RETRY_SEND,
    topology.QUEUE_DEAD_LETTERS,
    topology.QUEUE_REPORTS,
)


def test_exchanges_exist_and_are_durable(broker):
    declared = {item['name']: item for item in Rabbit.exchanges()}
    for name in (topology.EXCHANGE_EVENTS, topology.EXCHANGE_RETRY, topology.EXCHANGE_DLX):
        assert name in declared, f'Нет точки обмена {name}'
        assert declared[name]['type'] == 'topic'
        assert declared[name]['durable'] is True


def test_queues_exist_and_are_durable(broker):
    for name in EXPECTED_QUEUES:
        queue = Rabbit.queue(name)
        assert queue['durable'] is True, f'Очередь {name} не переживёт перезагрузку брокера'


def test_work_queues_dead_letter_to_dlx(broker):
    for name in (topology.QUEUE_FANOUT, topology.QUEUE_BUILD_EMAIL, topology.QUEUE_SEND_EMAIL):
        arguments = Rabbit.queue(name).get('arguments', {})
        assert arguments.get('x-dead-letter-exchange') == topology.EXCHANGE_DLX


def test_each_retry_tier_bounces_back_to_its_own_queue(broker):
    """Парковка без консьюмера: TTL истекает и возвращает сообщение в работу.

    Ярусов два, и ключ возврата у каждого свой. Общий ключ отправлял бы
    отложенное до утра письмо на ПОВТОРНУЮ СБОРКУ, то есть на лишний поход в
    Auth: при TTL в 30 секунд восьмичасовое ночное окно — почти тысяча запросов
    на каждого получателя.
    """
    tiers = (
        (topology.QUEUE_RETRY_BUILD, settings.NOTIFY_RETRY_BUILD_TTL_MS, topology.RK_NOTIFICATION_REQUESTED),
        (topology.QUEUE_RETRY_SEND, settings.NOTIFY_RETRY_TTL_MS, topology.RK_NOTIFICATION_PREPARED),
    )
    for name, ttl, routing_key in tiers:
        queue = Rabbit.queue(name)
        arguments = queue.get('arguments', {})
        assert arguments.get('x-message-ttl') == ttl
        assert arguments.get('x-dead-letter-exchange') == topology.EXCHANGE_EVENTS
        assert arguments.get('x-dead-letter-routing-key') == routing_key
        assert queue.get('consumers', 0) == 0


def test_send_queue_is_bound_only_to_prepared_letters(broker):
    """Привязка предыдущей версии снята, а не просто перестала объявляться.

    `queue_bind` аддитивен: забытая привязка к `.requested` пережила бы
    обновление, и отправляющий воркер получал бы пачки, предназначенные
    формирующему, — находил бы в них пустой список писем и молча подтверждал.
    """
    keys = {binding['routing_key'] for binding in Rabbit.bindings(topology.QUEUE_SEND_EMAIL)}
    assert topology.RK_NOTIFICATION_PREPARED in keys
    assert topology.RK_NOTIFICATION_REQUESTED not in keys


def test_build_queue_receives_the_fan_out_batches(broker):
    keys = {binding['routing_key'] for binding in Rabbit.bindings(topology.QUEUE_BUILD_EMAIL)}
    assert topology.RK_NOTIFICATION_REQUESTED in keys


def test_reports_queue_does_not_shadow_work_messages(broker):
    """Очередь отчётов привязана поимённо, а не маской.

    Под маской `notification-reporting.v1.*` в неё попадали бы и `.requested`, и
    `.prepared`, то есть каждая рабочая пачка ложилась бы второй копией в
    очередь, которую никто не разгребает.
    """
    keys = {binding['routing_key'] for binding in Rabbit.bindings(topology.QUEUE_REPORTS)}
    assert topology.RK_NOTIFICATION_REQUESTED not in keys
    assert topology.RK_NOTIFICATION_PREPARED not in keys
    assert {topology.RK_NOTIFICATION_DELIVERED, topology.RK_NOTIFICATION_FAILED} <= keys


def test_reports_carry_an_expiration(campaign, subscriber):
    """У отчёта есть срок жизни, и задан он НА СООБЩЕНИИ.

    Очередь отчётов не имеет консьюмера по замыслу, поэтому без ограничения она
    растёт всё время работы сервиса и однажды упирается в диск брокера, унося
    рабочие очереди вместе с собой. Аргументом очереди это уже не задать: она
    объявлена на всех работающих стендах, а переобъявление с новыми аргументами
    — 406 и краш-цикл воркеров. Обычная опасность per-message TTL (очередь
    протухает только с головы) здесь не возникает: срок у всех отчётов один.
    """
    launch_now(campaign)
    Mailpit.wait_for(count=1)
    Rabbit.wait_for_depth(topology.QUEUE_REPORTS, at_least=1)

    message = Rabbit.peek(topology.QUEUE_REPORTS)
    assert message is not None, 'отчёт не доехал до своей очереди'
    assert message['properties'].get('expiration') == str(settings.NOTIFY_REPORT_TTL_MS)
