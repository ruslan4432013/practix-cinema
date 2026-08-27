"""Доставка в открытую вкладку через websocket-шлюз.

## Что здесь на самом деле происходит

Отправитель НЕ держит соединений с браузерами и не знает о них. Он публикует
push в fanout-обменник ``notifications.ws``, а раздают его по сокетам реплики
``apps/notifications-ws/`` — отдельный асинхронный сервис. Иначе и быть не могло:
теория прямо предупреждает, что websocket требует асинхронных инструментов, а
этот воркер — синхронный процесс, который половину времени проводит внутри
``smtplib``.

## Успех означает «опубликовано», а не «показано»

``send()`` завершается успешно, как только брокер принял push, — даже если
адресат сейчас не онлайн и его никто не увидит. Это не поблажка, а разделение
ответственности: ДОЛГОВЕЧНАЯ доставка websocket-канала — строка в
``inbox_message``, которую пишет ``services/delivery.py`` в одной транзакции с
переводом задачи в «отправлено», а сокет — быстрый путь поверх неё. Человек,
закрывший вкладку, увидит уведомление в ленте кабинета.

Ровно поэтому push публикуется ``mandatory=False`` и транзиентным
(см. ``BrokerConnection.publish``): без единого поднятого шлюза у fanout-обменника
нет привязанных очередей, и ``mandatory=True`` превратил бы каждое уведомление в
бесконечный ярус повторов из-за отсутствующего необязательного контейнера.

## Порядок и дубликаты

Push уходит ДО коммита транзакции ``sent`` — так устроен контракт ``Sender``.
Следствий два, и оба закрыты клиентом:

* строка ленты появляется на миллисекунды позже кадра в сокете;
* при передоставке пачки из брокера push опубликуется второй раз.

Поэтому в кадре есть ``task_id``: он же лежит в ленте (``InboxMessage.task`` —
``OneToOne``), и клиент дедуплицирует по нему и кадры, и результаты догона.

## Чего в кадре нет

Тела письма. По той же причине, по которой его нет в ленте: рассылка на сто
тысяч человек — это сто тысяч копий одного HTML, и шлюз не должен стать местом,
где они всё-таки поедут по сети.
"""

import logging
from datetime import UTC, datetime

from practix_notifications.broker import topology
from practix_notifications.broker.publisher import BrokerConnection, PublishFailed
from practix_notifications.channels.base import (
    RenderedMessage,
    Sender,
    TemporaryDeliveryError,
)
from practix_notifications.core.config import settings
from practix_notifications.enums import Channel

logger = logging.getLogger('notifications.websocket')

#: Версия конверта push'а. Ровняется на ``NotificationPush.SCHEMA_VERSION`` в
#: шлюзе; расхождение там означает «отбросить с предупреждением», а не «разобрать
#: как получится». Две копии конверта — намеренно, см. модуль ``models/push.py``
#: шлюза: продюсер обязан падать на неполном конверте, консьюмер — не падать
#: никогда, и один класс не может вести себя обоими способами.
PUSH_SCHEMA_VERSION = 1


class WebsocketSender(Sender):
    channel = Channel.WEBSOCKET.value

    def __init__(self, broker: BrokerConnection | None = None) -> None:
        #: Соединение воркера переиспользуется, когда его передали: канал pika не
        #: потокобезопасен, но здесь всё в одном потоке, а вторая AMQP-сессия на
        #: процесс — это лишний файловый дескриптор и лишний heartbeat.
        self._broker = broker
        self._owns_broker = broker is None

    def open(self) -> None:
        if self._broker is None:
            self._broker = BrokerConnection()

    def close(self) -> None:
        if self._owns_broker and self._broker is not None:
            self._broker.close()
            self._broker = None

    def send(self, address: str, message: RenderedMessage) -> None:
        """``address`` для этого канала — идентификатор подписчика, а не почта.

        Так его проставляет формирующий воркер: у websocket-получателя нет
        «адреса» в почтовом смысле, а адресуется он ровно тем же UUID, что и в
        Auth. Проверка «адрес не пуст» в отправляющем воркере при этом остаётся
        осмысленной: пустой здесь означает, что получателя не удалось опознать.
        """
        self.open()
        assert self._broker is not None
        try:
            self._broker.publish(
                exchange=topology.EXCHANGE_WS,
                # У fanout-обменника ключ маршрутизации игнорируется: адресата
                # выбирает не брокер, а реплика шлюза — по user_id в теле.
                routing_key='',
                body=self._frame(address, message),
                mandatory=False,
                persistent=False,
            )
        except PublishFailed as exc:
            # Брокер лежит — это ровно тот отказ, который лечится повтором.
            raise TemporaryDeliveryError(f'Не удалось опубликовать push: {exc}') from exc

    def _frame(self, address: str, message: RenderedMessage) -> dict:
        # Импорт внутри функции, и это не лень: ``inbox.services`` импортирует
        # ``RenderedMessage`` из пакета ``channels``, поэтому импорт на уровне
        # модуля замкнул бы цикл channels → inbox → channels.
        from practix_notifications.inbox.services import build_preview

        return {
            'schema_version': PUSH_SCHEMA_VERSION,
            'user_id': address,
            'task_id': message.headers.get('X-Task-Id', ''),
            'subject': message.subject,
            'preview': build_preview(message, limit=settings.NOTIFY_INBOX_PREVIEW_CHARS),
            'channel': self.channel,
            'sent_at': datetime.now(UTC).isoformat(),
        }
