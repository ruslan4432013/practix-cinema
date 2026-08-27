"""Каналы доставки: реестр, заглушки, SMTP-соединение, websocket-push и пул."""

import smtplib

import pytest

from practix_notifications import channels
from practix_notifications.broker import topology
from practix_notifications.broker.publisher import PublishFailed
from practix_notifications.channels import ChannelNotImplemented, RenderedMessage, SmtpSender
from practix_notifications.channels.base import TemporaryDeliveryError
from practix_notifications.enums import Channel


class FakeSMTP:
    """Учёт вызовов вместо настоящего почтового сервера."""

    instances: list['FakeSMTP'] = []

    def __init__(self, host=None, port=None, timeout=None):
        self.sent: list = []
        self.quit_called = False
        FakeSMTP.instances.append(self)

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def send_message(self, message):
        self.sent.append(message)

    def quit(self):
        self.quit_called = True


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, 'SMTP', FakeSMTP)
    return FakeSMTP


def message(subject='Тема') -> RenderedMessage:
    return RenderedMessage(subject=subject, body='<p>Привет</p>', is_html=True, headers={'X-Idempotency-Key': 'k1'})


def test_connection_is_reused_across_the_batch(fake_smtp):
    """Замер теории: коннект ≈ 5 с против < 1 с на письмо.

    Отправитель, подключающийся на каждое письмо, тратит на подключение впятеро
    больше, чем на работу.
    """
    sender = SmtpSender()
    with sender:
        for index in range(5):
            sender.send(f'user{index}@example.com', message())

    assert len(fake_smtp.instances) == 1
    assert sender.connects == 1
    assert len(fake_smtp.instances[0].sent) == 5


def test_connection_reopens_after_disconnect(fake_smtp):
    sender = SmtpSender()
    sender.open()

    def explode(_message):
        raise smtplib.SMTPServerDisconnected('boom')

    fake_smtp.instances[0].send_message = explode
    sender.send('user@example.com', message())

    assert sender.connects == 2
    assert len(fake_smtp.instances[1].sent) == 1


def test_smtp_failure_is_temporary(fake_smtp):
    sender = SmtpSender()
    sender.open()
    fake_smtp.instances[0].send_message = _raise(smtplib.SMTPDataError(451, b'try later'))
    with pytest.raises(TemporaryDeliveryError):
        sender.send('user@example.com', message())


def test_html_message_carries_plain_text_alternative(fake_smtp):
    sender = SmtpSender()
    with sender:
        sender.send('user@example.com', message())
    mail = fake_smtp.instances[0].sent[0]
    assert mail.is_multipart()
    assert {part.get_content_subtype() for part in mail.iter_parts()} == {'plain', 'html'}


def test_custom_headers_reach_the_letter(fake_smtp):
    sender = SmtpSender()
    with sender:
        sender.send('user@example.com', message())
    assert fake_smtp.instances[0].sent[0]['X-Idempotency-Key'] == 'k1'


def test_registry_returns_email_sender():
    assert isinstance(channels.get_sender(Channel.EMAIL.value), SmtpSender)


@pytest.mark.parametrize('channel', [Channel.PUSH.value, Channel.SMS.value])
def test_unimplemented_channels_fail_loudly(channel):
    """Отказ должен быть видимым и с причиной, а не тихой потерей письма."""
    sender = channels.get_sender(channel)
    with pytest.raises(ChannelNotImplemented) as exc:
        sender.send('user@example.com', message())
    assert channel in str(exc.value)
    assert not channels.is_implemented(channel)


def test_unknown_channel_rejected():
    with pytest.raises(ChannelNotImplemented):
        channels.get_sender('telepathy')


# --- websocket ---------------------------------------------------------------


class FakeBroker:
    """Учёт публикаций вместо RabbitMQ."""

    def __init__(self, explode: Exception | None = None) -> None:
        self.published: list[dict] = []
        self._explode = explode
        self.closed = False

    def publish(self, **kwargs):
        if self._explode is not None:
            raise self._explode
        self.published.append(kwargs)

    def close(self):
        self.closed = True


@pytest.mark.django_db
def test_websocket_push_goes_to_the_fanout_exchange():
    broker = FakeBroker()
    sender = channels.WebsocketSender(broker)
    sender.send('11111111-1111-1111-1111-111111111111', message())

    (call,) = broker.published
    assert call['exchange'] == topology.EXCHANGE_WS
    # У fanout-обменника ключ игнорируется: адресата выбирает реплика шлюза.
    assert call['routing_key'] == ''


@pytest.mark.django_db
def test_websocket_push_is_neither_mandatory_nor_persistent():
    """Ровно то, на чём этот канал ломался бы иначе.

    ``mandatory=True`` без единого поднятого шлюза даёт UnroutableError на
    КАЖДОЕ уведомление: у fanout-обменника нет привязанных очередей, пока никто
    не подписан, — и необязательный контейнер превратился бы в бесконечный ярус
    повторов. ``persistent`` не нужен по смыслу: push, который некому показать,
    через минуту бесполезен, а долговечная копия лежит в ленте.
    """
    broker = FakeBroker()
    channels.WebsocketSender(broker).send('user-1', message())

    (call,) = broker.published
    assert call['mandatory'] is False
    assert call['persistent'] is False


@pytest.mark.django_db
def test_websocket_push_carries_task_id_and_preview_but_not_the_body():
    broker = FakeBroker()
    rendered = RenderedMessage(
        subject='Новый фильм',
        body='<p>Смотрите <b>Дюну</b></p>',
        is_html=True,
        headers={'X-Task-Id': 'task-42'},
    )
    channels.WebsocketSender(broker).send('user-1', rendered)

    body = broker.published[0]['body']
    assert body['user_id'] == 'user-1'
    # Ключ склейки кадра со строкой ленты — без него клиент показал бы дубль.
    assert body['task_id'] == 'task-42'
    assert body['preview'] == 'Смотрите Дюну'
    # Тела письма в кадре нет: сто тысяч получателей — это сто тысяч копий HTML.
    assert 'body' not in body


@pytest.mark.django_db
def test_broker_failure_is_temporary():
    """Брокер лежит — это ровно тот отказ, который лечится повтором."""
    sender = channels.WebsocketSender(FakeBroker(explode=PublishFailed('broker down')))
    with pytest.raises(TemporaryDeliveryError):
        sender.send('user-1', message())


def test_websocket_is_implemented():
    assert channels.is_implemented(Channel.WEBSOCKET.value)
    assert channels.is_implemented(Channel.EMAIL.value)


# --- пул отправителей --------------------------------------------------------


@pytest.mark.django_db
def test_pool_keeps_one_smtp_connection_for_the_whole_batch(fake_smtp):
    """Инвариант из channels/email.py не должен был пострадать от пула."""
    with channels.SenderPool(broker=FakeBroker()) as pool:
        for index in range(5):
            pool.get(Channel.EMAIL.value).send(f'user{index}@example.com', message())

    assert len(fake_smtp.instances) == 1


@pytest.mark.django_db
def test_pool_routes_each_channel_to_its_own_sender(fake_smtp):
    """Ради этого пул и появился: websocket-задача не должна уйти в SMTP."""
    broker = FakeBroker()
    with channels.SenderPool(broker=broker) as pool:
        pool.get(Channel.EMAIL.value).send('user@example.com', message())
        pool.get(Channel.WEBSOCKET.value).send('user-1', message())

    assert len(fake_smtp.instances[0].sent) == 1
    assert len(broker.published) == 1


@pytest.mark.django_db
def test_pool_defers_channels_it_does_not_serve():
    """Отдельный воркер на канал остаётся возможен — и не теряет чужие задачи.

    ``channel_unavailable`` здесь был бы потерей: канал реализован, доставить
    его обязан соседний процесс. Поэтому временный отказ, а не пропуск.
    """
    pool = channels.SenderPool(allowed={Channel.EMAIL.value}, broker=FakeBroker())
    with pytest.raises(TemporaryDeliveryError):
        pool.get(Channel.WEBSOCKET.value)


def _raise(exc):
    def _inner(_message):
        raise exc

    return _inner
