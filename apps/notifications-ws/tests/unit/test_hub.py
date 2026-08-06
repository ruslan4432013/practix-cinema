"""Реестр соединений: адресность, лимиты и честное признание потерь."""

import pytest

from practix_notifications_ws.services.hub import ConnectionHub, ConnectionLimitReached

FRAME = {'type': 'notification', 'data': {'task_id': 't1'}}


def test_push_reaches_only_the_addressee(hub: ConnectionHub):
    """Главное свойство шлюза: чужих уведомлений не бывает."""
    mine = hub.subscribe('user-a')
    theirs = hub.subscribe('user-b')

    assert hub.publish('user-a', FRAME) == 1

    assert mine.queue.get_nowait() == FRAME
    assert theirs.queue.empty()


def test_all_tabs_of_one_person_get_the_frame(hub: ConnectionHub):
    first, second = hub.subscribe('user-a'), hub.subscribe('user-a')

    assert hub.publish('user-a', FRAME) == 2

    assert first.queue.qsize() == second.queue.qsize() == 1


def test_push_to_nobody_is_not_an_error(hub: ConnectionHub):
    """Адресат не онлайн — обычное дело: его копия уже лежит в ленте кабинета."""
    assert hub.publish('user-a', FRAME) == 0


def test_connection_limit_per_user(hub: ConnectionHub):
    hub.subscribe('user-a')
    hub.subscribe('user-a')
    with pytest.raises(ConnectionLimitReached):
        hub.subscribe('user-a')


def test_total_connection_limit(hub: ConnectionHub):
    hub.subscribe('user-a')
    hub.subscribe('user-a')
    hub.subscribe('user-b')
    hub.subscribe('user-b')
    with pytest.raises(ConnectionLimitReached):
        hub.subscribe('user-c')


def test_long_poll_ignores_the_limits(hub: ConnectionHub):
    """Клиент, дошедший до long polling'а, УЖЕ деградировал.

    Отказать ему по лимиту вкладок значило бы отправить его на ленту с минутными
    паузами ровно в тот момент, когда он и так остался без сокета.
    """
    hub.subscribe('user-a')
    hub.subscribe('user-a')

    poller = hub.subscribe('user-a', count_towards_limits=False)

    assert hub.publish('user-a', FRAME) == 3
    assert poller.queue.qsize() == 1


def test_unsubscribe_frees_the_slot(hub: ConnectionHub):
    first = hub.subscribe('user-a')
    hub.subscribe('user-a')
    hub.unsubscribe(first)

    hub.subscribe('user-a')  # не должно бросить


def test_unsubscribe_is_idempotent(hub: ConnectionHub):
    """Закрытие сокета бывает двойным: onclose плюс finally в обработчике.

    Второй вызов не должен уводить счётчик в минус — иначе лимит соединений
    перестал бы срабатывать вовсе.
    """
    subscription = hub.subscribe('user-a')
    hub.unsubscribe(subscription)
    hub.unsubscribe(subscription)

    assert hub.total == 0
    assert hub.users() == 0


def test_empty_bucket_is_removed(hub: ConnectionHub):
    """Иначе реестр превратился бы в список всех, кто когда-либо подключался."""
    hub.unsubscribe(hub.subscribe('user-a'))
    assert hub.users() == 0


def test_overflow_drops_the_oldest_and_counts_it(hub: ConnectionHub):
    """Медленный клиент не должен раздувать память процесса — но и молча терять
    кадры нельзя: человек увидел бы ленту с дырой и не узнал бы об этом."""
    subscription = hub.subscribe('user-a')
    for index in range(5):  # очередь на 3
        hub.publish('user-a', {'type': 'notification', 'data': {'task_id': f't{index}'}})

    assert subscription.queue.qsize() == 3
    assert subscription.dropped == 2
    # Выброшены САМЫЕ СТАРЫЕ: свежее уведомление ценнее протухшего.
    assert subscription.queue.get_nowait()['data']['task_id'] == 't2'


def test_taking_dropped_resets_the_counter(hub: ConnectionHub):
    subscription = hub.subscribe('user-a')
    for _ in range(5):
        hub.publish('user-a', FRAME)

    assert subscription.take_dropped() == 2
    assert subscription.take_dropped() == 0


class TestConnectionBudget:
    """Поллеры не расходуют бюджет соединений, сокеты — расходуют.

    Раньше ``count_towards_limits=False`` пропускал только ПРОВЕРКИ, а счётчик
    увеличивался всё равно. Достаточное число одновременных ``/poll`` упирало
    ``total`` в ``max_total``, после чего шлюз переставал пускать настоящие
    сокеты: деградировавший клиент выбивал недеградировавших.
    """

    def test_pollers_do_not_consume_the_socket_budget(self, hub: ConnectionHub):
        for index in range(10):
            hub.subscribe(f'poller-{index}', count_towards_limits=False)

        assert hub.total == 0
        assert hub.pollers == 10
        # Бюджет цел: сокет по-прежнему принимается.
        assert hub.subscribe('user-a') is not None

    def test_sockets_still_consume_it(self, hub: ConnectionHub):
        hub.subscribe('user-a')
        hub.subscribe('user-a')
        hub.subscribe('user-b')
        hub.subscribe('user-b')

        assert hub.total == 4
        with pytest.raises(ConnectionLimitReached):
            hub.subscribe('user-c')

    def test_unsubscribe_returns_what_subscribe_took(self, hub: ConnectionHub):
        socket = hub.subscribe('user-a')
        poller = hub.subscribe('user-a', count_towards_limits=False)

        hub.unsubscribe(poller)
        assert (hub.total, hub.pollers) == (1, 0)

        hub.unsubscribe(socket)
        assert (hub.total, hub.pollers) == (0, 0)

    def test_repeated_unsubscribe_does_not_go_negative(self, hub: ConnectionHub):
        poller = hub.subscribe('user-a', count_towards_limits=False)
        hub.unsubscribe(poller)
        hub.unsubscribe(poller)

        assert hub.pollers == 0

    def test_a_poller_still_receives_frames(self, hub: ConnectionHub):
        """Не считаться в бюджете — не то же самое, что не получать уведомлений."""
        poller = hub.subscribe('user-a', count_towards_limits=False)

        assert hub.publish('user-a', FRAME) == 1
        assert poller.queue.get_nowait() == FRAME
