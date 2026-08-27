"""Слив outbox: две темы, и вторая важнее первой.

**Блокировка строки не имеет права переживать разговор с брокером.** Раньше
``select_for_update`` и ``basic_publish`` стояли в одной транзакции, то есть
молчащий брокер удерживал и блокировку, и соединение с Postgres всё время
round-trip — а он ограничен только heartbeat, то есть минутами. Теперь строка
проходит захват → публикацию → фиксацию, и транзакция открыта только на первом
шаге; взаимное исключение между репликами держит ``available_at``.

**Одна недоставляемая строка не имеет права остановить рассылки.**
``PublishFailed`` приходит по двум разным поводам, и они наконец различимы:
``BrokerUnavailable`` лечится ожиданием (следующие строки упрутся в то же самое —
слив останавливается), ``Unroutable`` не лечится ничем (брокер жив — идём к
следующей строке). Потолок попыток остаётся страховкой для обоих, но теперь
сопровождается отсрочкой: без неё десять попыток сгорали за десять секунд
недоступности брокера.
"""

from datetime import UTC, datetime, timedelta

import pytest
from django.db import transaction

from practix_notifications.broker import topology
from practix_notifications.broker.publisher import PublishFailed, Unroutable
from practix_notifications.campaigns.models import OutboxMessage
from practix_notifications.core.config import settings
from practix_notifications.services import outbox

pytestmark = pytest.mark.django_db


class PickyBroker:
    """Брокер, который отказывается публиковать сообщения с заданным ключом.

    ``failure`` выбирает разновидность отказа: по умолчанию базовый
    ``PublishFailed``, который слив обязан трактовать консервативно — как
    «брокер недоступен».
    """

    def __init__(self, *, poison_key: str = '', failure: type[Exception] = PublishFailed) -> None:
        self.poison_key = poison_key
        self.failure = failure
        self.published: list[str] = []
        #: Была ли открыта транзакция в момент публикации — см. отдельный тест.
        self.saw_atomic_block: list[bool] = []

    def publish(self, *, exchange: str, routing_key: str, body: dict, headers: dict | None = None) -> None:
        self.saw_atomic_block.append(transaction.get_connection().in_atomic_block)
        if self.poison_key and routing_key == self.poison_key:
            raise self.failure(f'отказ на ключе {routing_key!r}')
        self.published.append(routing_key)


def _enqueue(routing_key: str) -> OutboxMessage:
    return OutboxMessage.objects.create(exchange=topology.EXCHANGE_EVENTS, routing_key=routing_key, body={'x': 1})


def test_healthy_messages_are_published_and_marked():
    _enqueue(topology.RK_CAMPAIGN_LAUNCHED)
    broker = PickyBroker()

    assert outbox.drain(broker, limit=10) == 1
    assert OutboxMessage.objects.filter(published_at__isnull=True).count() == 0


@pytest.mark.django_db(transaction=True)
def test_publish_happens_outside_a_transaction():
    """Собственно правка: разговор с брокером не держит ни блокировку, ни транзакцию.

    ``transaction=True`` здесь обязателен. Модульный ``pytestmark`` заворачивает
    каждый тест в откатываемый atomic-блок, и под ним ``in_atomic_block`` был бы
    ``True`` и до правки, и после — тест доказывал бы ровно ничего.
    """
    _enqueue(topology.RK_CAMPAIGN_LAUNCHED)
    broker = PickyBroker()

    assert outbox.drain(broker, limit=10) == 1
    assert broker.saw_atomic_block == [False]


def test_broker_outage_stops_the_drain_without_burning_the_queue():
    """Отказ брокера — не повод перебирать всю очередь и писать в лог копии одной ошибки."""
    for _ in range(3):
        _enqueue(topology.RK_CAMPAIGN_LAUNCHED)
    broker = PickyBroker(poison_key=topology.RK_CAMPAIGN_LAUNCHED)

    assert outbox.drain(broker, limit=10) == 0
    assert OutboxMessage.objects.filter(attempts=1).count() == 1, 'попытку тратит только голова'


def test_broker_outage_does_not_burn_the_budget_at_tick_rate():
    """Главная правка потолка попыток: он измеряется выносливостью, а не секундами.

    Слив крутится раз в секунду. Без отсрочки десять тиков подряд сжигали весь
    бюджет исправной строки за десять секунд недоступности брокера, после чего
    она выпадала из выборки навсегда — сбросить ``attempts`` можно только руками
    в базе.
    """
    _enqueue(topology.RK_CAMPAIGN_LAUNCHED)
    broker = PickyBroker(poison_key=topology.RK_CAMPAIGN_LAUNCHED)

    for _ in range(5):
        assert outbox.drain(broker, limit=10) == 0

    message = OutboxMessage.objects.get()
    assert message.attempts == 1, 'отложенную строку следующие тики не трогают'
    assert message.available_at > datetime.now(UTC)


def test_unroutable_message_does_not_block_the_ones_behind_it():
    """Брокер жив, плоха одна строка — остальные обязаны уехать тем же тиком.

    Раньше различить «брокер лежит» и «этому ключу некуда» было нельзя, поэтому
    ядовитая строка вставала в голову выборки по ``created_at`` и держала все
    рассылки, пока не исчерпает потолок попыток.
    """
    poison = _enqueue('reporting.v1.nowhere')
    healthy = _enqueue(topology.RK_CAMPAIGN_LAUNCHED)
    broker = PickyBroker(poison_key='reporting.v1.nowhere', failure=Unroutable)

    assert outbox.drain(broker, limit=10) == 1
    assert broker.published == [topology.RK_CAMPAIGN_LAUNCHED]

    healthy.refresh_from_db()
    poison.refresh_from_db()
    assert healthy.published_at is not None
    assert poison.attempts == 1
    assert poison.published_at is None, 'строка остаётся в базе — её нужно разобрать глазами'


def test_undeliverable_message_falls_out_of_the_selection_after_the_budget():
    """Потолок попыток остаётся страховкой: строка выбывает и перестаёт занимать тик."""
    poison = _enqueue('reporting.v1.nowhere')
    broker = PickyBroker(poison_key='reporting.v1.nowhere', failure=Unroutable)

    for _ in range(settings.NOTIFY_OUTBOX_MAX_ATTEMPTS):
        # Отсрочку отматываем назад: имитируем тики, разнесённые во времени.
        OutboxMessage.objects.filter(pk=poison.pk).update(available_at=None)
        outbox.drain(broker, limit=10)

    poison.refresh_from_db()
    assert poison.attempts == settings.NOTIFY_OUTBOX_MAX_ATTEMPTS
    assert poison.published_at is None

    OutboxMessage.objects.filter(pk=poison.pk).update(available_at=None)
    assert outbox.drain(broker, limit=10) == 0
    assert broker.published == [], 'исчерпавшую бюджет строку слив больше не берёт'


def test_a_claimed_row_is_invisible_to_a_second_replica():
    """То, ради чего раньше держали блокировку строки: два планировщика, одна строка."""
    _enqueue(topology.RK_CAMPAIGN_LAUNCHED)
    OutboxMessage.objects.update(available_at=datetime.now(UTC) + timedelta(seconds=60))
    broker = PickyBroker()

    assert outbox.drain(broker, limit=10) == 0
    assert broker.published == []


def test_an_expired_lease_returns_the_row_to_the_drain():
    """Реплика, умершая внутри публикации, не хоронит строку — её подберут после аренды."""
    _enqueue(topology.RK_CAMPAIGN_LAUNCHED)
    OutboxMessage.objects.update(available_at=datetime.now(UTC) - timedelta(seconds=1))
    broker = PickyBroker()

    assert outbox.drain(broker, limit=10) == 1
    assert OutboxMessage.objects.get().published_at is not None


def test_an_unexpected_error_burns_an_attempt_and_propagates():
    """Не ``PublishFailed`` — тоже попытка, иначе строка загораживает очередь вечно.

    Раньше такое исключение (например ``TypeError`` из ``json.dumps`` на
    несериализуемом теле) откатывало транзакцию вместе с инкрементом попыток, и
    один и тот же traceback падал каждый тик, ничего не двигая.
    """

    class BrokenBroker:
        def publish(self, **_: object) -> None:
            raise TypeError('тело не сериализуется')

    _enqueue(topology.RK_CAMPAIGN_LAUNCHED)

    with pytest.raises(TypeError):
        outbox.drain(BrokenBroker(), limit=10)

    message = OutboxMessage.objects.get()
    assert message.attempts == 1
    assert message.published_at is None
