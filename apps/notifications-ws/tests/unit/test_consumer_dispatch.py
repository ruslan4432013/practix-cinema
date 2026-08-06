"""Разбор сообщений из брокера: кривое не роняет шлюз.

Проверяется ``_dispatch`` без сети: подключение к RabbitMQ — это уже
функциональный набор нотификаций, а здесь важна ровно одна гарантия — исключение
из разбора не выходит наружу. Выйди оно — оборвалось бы AMQP-соединение, а
вместе с ним все открытые сокеты реплики из-за ОДНОГО плохого сообщения.
"""

import json

from practix_notifications_ws.brokers.consumer import PushConsumer
from practix_notifications_ws.models.push import SCHEMA_VERSION
from practix_notifications_ws.services.hub import ConnectionHub

VALID = {'schema_version': SCHEMA_VERSION, 'user_id': 'user-1', 'task_id': 'task-1', 'subject': 'Тема'}


def _consumer(hub: ConnectionHub) -> PushConsumer:
    return PushConsumer(hub)


def test_push_reaches_the_addressee(hub: ConnectionHub):
    subscription = hub.subscribe('user-1')
    consumer = _consumer(hub)

    consumer._dispatch(json.dumps(VALID).encode())

    assert subscription.queue.get_nowait()['data']['task_id'] == 'task-1'
    assert consumer.stats['delivered'] == 1


def test_offline_addressee_is_counted_separately(hub: ConnectionHub):
    """Не ошибка: копия уведомления уже лежит в ленте кабинета. Счётчик нужен,
    чтобы «шлюз молчит» отличалось от «до шлюза ничего не доезжает»."""
    consumer = _consumer(hub)

    consumer._dispatch(json.dumps(VALID).encode())

    assert consumer.stats == {'delivered': 0, 'undeliverable': 1}


def test_broken_json_is_dropped_not_raised(hub: ConnectionHub):
    _consumer(hub)._dispatch(b'{not json')


def test_envelope_of_unknown_version_is_dropped(hub: ConnectionHub):
    _consumer(hub)._dispatch(json.dumps({**VALID, 'schema_version': 99}).encode())


def test_envelope_without_addressee_is_dropped(hub: ConnectionHub):
    _consumer(hub)._dispatch(json.dumps({'task_id': 'task-1'}).encode())


def test_bad_message_does_not_break_the_next_one(hub: ConnectionHub):
    """Главное свойство: пайплайн продолжает работать после отравленного кадра."""
    subscription = hub.subscribe('user-1')
    consumer = _consumer(hub)

    consumer._dispatch(b'garbage')
    consumer._dispatch(json.dumps(VALID).encode())

    assert subscription.queue.qsize() == 1
