"""Слив outbox: одна недоставляемая строка не имеет права остановить рассылки.

``PublishFailed`` приходит по двум разным поводам. «Брокер недоступен» лечится
ожиданием, и остановить слив на первой же ошибке — правильный ответ: следующие
строки упрутся в то же самое. «У этого ключа маршрутизации нет ни одной
привязки» (``UnroutableError``) ожиданием не лечится никогда, а выборка идёт по
``created_at`` — поэтому такая строка встаёт в голову и молча останавливает
публикацию ВСЕХ рассылок навсегда.

Различить их по исключению нельзя, поэтому у строки есть потолок попыток. Голова
разблокируется максимум за ``NOTIFY_OUTBOX_MAX_ATTEMPTS`` тиков, строка остаётся
в базе для разбора и попадает в ``/health/ready`` отдельным счётчиком.
"""

import pytest

from practix_notifications.broker import topology
from practix_notifications.broker.publisher import PublishFailed
from practix_notifications.campaigns.models import OutboxMessage
from practix_notifications.core.config import settings
from practix_notifications.services import outbox

pytestmark = pytest.mark.django_db


class PickyBroker:
    """Брокер, который отказывается публиковать сообщения с заданным ключом."""

    def __init__(self, *, poison_key: str = '') -> None:
        self.poison_key = poison_key
        self.published: list[str] = []

    def publish(self, *, exchange: str, routing_key: str, body: dict, headers: dict | None = None) -> None:
        if self.poison_key and routing_key == self.poison_key:
            raise PublishFailed(f'нет привязки под ключ {routing_key!r}')
        self.published.append(routing_key)


def _enqueue(routing_key: str) -> OutboxMessage:
    return OutboxMessage.objects.create(exchange=topology.EXCHANGE_EVENTS, routing_key=routing_key, body={'x': 1})


def test_healthy_messages_are_published_and_marked():
    _enqueue(topology.RK_CAMPAIGN_LAUNCHED)
    broker = PickyBroker()

    assert outbox.drain(broker, limit=10) == 1
    assert OutboxMessage.objects.filter(published_at__isnull=True).count() == 0


def test_broker_outage_stops_the_drain_without_burning_the_queue():
    """Отказ брокера — не повод перебирать всю очередь и писать в лог копии одной ошибки."""
    for _ in range(3):
        _enqueue(topology.RK_CAMPAIGN_LAUNCHED)
    broker = PickyBroker(poison_key=topology.RK_CAMPAIGN_LAUNCHED)

    assert outbox.drain(broker, limit=10) == 0
    assert OutboxMessage.objects.filter(attempts=1).count() == 1, 'попытку тратит только голова'


def test_undeliverable_message_stops_blocking_after_the_budget():
    """Главная правка: ядовитая строка выпадает из выборки, а не держит её вечно."""
    poison = _enqueue('reporting.v1.nowhere')
    healthy = _enqueue(topology.RK_CAMPAIGN_LAUNCHED)
    broker = PickyBroker(poison_key='reporting.v1.nowhere')

    # Каждый тик упирается в голову — ровно до тех пор, пока она не исчерпает потолок.
    for _ in range(settings.NOTIFY_OUTBOX_MAX_ATTEMPTS):
        outbox.drain(broker, limit=10)

    poison.refresh_from_db()
    assert poison.attempts == settings.NOTIFY_OUTBOX_MAX_ATTEMPTS
    assert poison.published_at is None, 'строка остаётся в базе — её нужно разобрать глазами'
    assert broker.published == [], 'пока голова не выбыла, за неё никто не проходит'

    # Следующий тик проходит мимо неё и наконец публикует то, что за ней стояло.
    assert outbox.drain(broker, limit=10) == 1
    healthy.refresh_from_db()
    assert healthy.published_at is not None
