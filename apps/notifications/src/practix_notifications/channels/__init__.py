"""Реестр отправителей: канал → класс, который умеет доставлять.

Реестр, а не ``if channel == 'email'`` в воркере: добавление канала не должно
трогать путь рассылки. Нереализованные каналы объявлены явными заглушками —
чтобы «мы этого пока не умеем» было видимым отказом с причиной в статистике, а
не тихой потерей письма.

Реализованы email и websocket. Задание разрешает ограничиться письмами («для
простоты можно рассылать только email-письма»), но требует, чтобы расширение
было возможно, — вот оно, и websocket его подтвердил на практике: появление
второго канала не потребовало ни новой модели, ни правки веера, ни правки
админки. Изменились ровно две вещи — эта таблица и то, что воркер теперь берёт
отправителя ПО КАНАЛУ ЗАДАЧИ (см. :class:`SenderPool`), а не по флагу запуска.

Push и SMS остаются заглушками: у обоих нет не кода, а внешней системы.
"""

from collections.abc import Callable

from practix_notifications.channels.base import (
    ChannelNotImplemented,
    PermanentDeliveryError,
    RenderedMessage,
    Sender,
    TemporaryDeliveryError,
    UnimplementedSender,
)
from practix_notifications.channels.email import SmtpSender
from practix_notifications.channels.websocket import WebsocketSender
from practix_notifications.enums import Channel

__all__ = [
    'ChannelNotImplemented',
    'PermanentDeliveryError',
    'RenderedMessage',
    'Sender',
    'SenderPool',
    'SmtpSender',
    'TemporaryDeliveryError',
    'WebsocketSender',
    'get_sender',
    'is_implemented',
]

#: Фабрики, а не готовые экземпляры: у отправителя есть соединение, и один
#: экземпляр на процесс-воркер — это решение воркера, а не реестра.
SENDERS: dict[str, Callable[[], Sender]] = {
    Channel.EMAIL.value: SmtpSender,
    Channel.WEBSOCKET.value: WebsocketSender,
    Channel.PUSH.value: lambda: UnimplementedSender(
        Channel.PUSH.value, 'нужен внешний push-сервис и хранилище токенов браузера'
    ),
    Channel.SMS.value: lambda: UnimplementedSender(
        Channel.SMS.value, 'нужен платный шлюз и телефоны пользователей, которых Auth не собирает'
    ),
}

#: Каналы, у которых есть НАСТОЯЩИЙ отправитель. Раньше это выражалось как
#: ``SENDERS[channel] is SmtpSender`` — приём, который работал ровно до второго
#: реализованного канала и молча соврал бы на третьем.
IMPLEMENTED: frozenset[str] = frozenset({Channel.EMAIL.value, Channel.WEBSOCKET.value})


def get_sender(channel: str) -> Sender:
    """Создать отправителя канала. Неизвестный канал — ошибка конфигурации."""
    factory = SENDERS.get(channel)
    if factory is None:
        raise ChannelNotImplemented(f'Неизвестный канал: {channel!r}')
    return factory()


def is_implemented(channel: str) -> bool:
    return channel in IMPLEMENTED


class SenderPool:
    """Отправители процесса-воркера, по одному на канал, созданные лениво.

    ## Зачем понадобился

    Пока реализованным был один канал, воркер создавал ОДИН отправитель по флагу
    ``--channel`` и звал его для любой задачи; задачи прочих каналов до него не
    доходили — их отсекала проверка ``is_implemented`` как ``channel_unavailable``.
    С появлением websocket этот приём стал опасным: пачка может содержать задачи
    обоих каналов, и websocket-уведомление ушло бы в SMTP.

    ## Почему пул, а не очередь на канал

    «Правильная топология» (``notifications.send-{channel}`` с привязкой по
    ключу канала и контейнером воркера на канал) остаётся отложенной, и это
    осознанный размен. Она переписывает привязки РАБОТАЮЩЕГО email-пути —
    ``queue_bind`` аддитивен, старую привязку пришлось бы явно снимать, ровно как
    уже сделано с ``LEGACY_SEND_BINDING``, — добавляет второй ярус повторов и
    пятый контейнер. Выигрыш, ради которого email когда-то получил свой процесс,
    здесь отсутствует: публикация push'а в AMQP не стоит пяти секунд, которые
    стоит коннект к SMTP.

    ## Что сохранено

    Дорогое соединение по-прежнему переживает пачку: экземпляр отправителя
    создаётся один раз на процесс и закрывается на остановке, поэтому
    ``SmtpSender.connects`` остаётся равным единице на любое число писем — тот же
    инвариант, что закреплён юнит-тестом.
    """

    def __init__(self, *, allowed: set[str] | None = None, broker=None) -> None:
        """:param allowed: каналы, которые ЭТОТ процесс берётся обрабатывать.
        Пустое значение — все реализованные. Существует, чтобы отдельный
        контейнер на канал остался возможен без правки кода.

        :param broker: соединение с брокером воркера. Передаётся websocket-
            отправителю, чтобы не открывать вторую AMQP-сессию на процесс.
        """
        self._allowed = allowed or set(IMPLEMENTED)
        self._broker = broker
        self._instances: dict[str, Sender] = {}

    def handles(self, channel: str) -> bool:
        return channel in self._allowed

    def get(self, channel: str) -> Sender:
        if channel in IMPLEMENTED and not self.handles(channel):
            # Канал реализован, но НЕ этим процессом. Это временный отказ, а не
            # `channel_unavailable`: пометить задачу пропущенной значило бы
            # потерять уведомление, которое обязан доставить соседний воркер.
            raise TemporaryDeliveryError(f'Канал {channel!r} обслуживает другой воркер')
        sender = self._instances.get(channel)
        if sender is None:
            sender = WebsocketSender(self._broker) if channel == Channel.WEBSOCKET.value else get_sender(channel)
            sender.open()
            self._instances[channel] = sender
        return sender

    def close(self) -> None:
        for sender in self._instances.values():
            sender.close()
        self._instances.clear()

    def __enter__(self) -> 'SenderPool':
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
