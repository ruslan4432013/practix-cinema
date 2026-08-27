"""Подписка на fanout-обменник ``notifications.ws``.

## Своя очередь у каждого процесса, и она недолговечная

Очередь объявляется ``exclusive=True``, ``auto_delete=True``, ``durable=False``,
с именем от сервера. Каждое из четырёх свойств отвечает за свою беду:

* **exclusive** — очередь принадлежит одному соединению, и никакая вторая реплика
  в неё не залезет. Обычная (не exclusive) очередь на несколько консьюмеров
  раздавала бы сообщения по очереди, и push доставался бы одной реплике из N —
  то есть в среднем не той, где сидит адресат;
* **auto_delete** — умерла реплика, исчезла очередь. Иначе в брокере копились бы
  очереди мёртвых процессов, каждая — с растущим бэклогом ненужных push'ей;
* **durable=False** и имя от сервера — очередь существует ровно пока живёт
  процесс, и переживать перезапуск брокера ей незачем.

## Объявление обменника обязано совпадать с продюсером

``fanout``, ``durable=True`` — как в
``practix_notifications.broker.topology``. Расхождение по типу или durability
даёт ``PRECONDITION_FAILED`` (406) и crash-loop у того, кто объявил вторым. Тот
же класс ловушки уже описан в сервисе нотификаций для очереди повторов.

## Сообщения подтверждаются сразу и никогда не возвращаются в очередь

``no_ack=True``. Уведомление, которое некому показать, бесполезно: адресат не
подключён, и повторная доставка ничего не изменит — она найдёт его так же
неподключённым. Долговечность здесь даёт не брокер, а ``inbox_message`` и
лента кабинета. Ровно поэтому же продюсер публикует push транзиентным
(``delivery_mode=1``).

## Кривое сообщение не роняет шлюз

Разбор изолирован: непонятный конверт логируется и отбрасывается. Исключение,
выпущенное наружу, оборвало бы AMQP-соединение и вместе с ним — все открытые
сокеты реплики из-за одного плохого сообщения.
"""

import asyncio
import contextlib
import json
import logging

import aio_pika

from practix_notifications_ws.core.config import settings
from practix_notifications_ws.models.push import NotificationPush, PushError
from practix_notifications_ws.services.hub import ConnectionHub

logger = logging.getLogger('notifications_ws.consumer')


class PushConsumer:
    """Фоновая задача: брокер → :class:`ConnectionHub`.

    Живёт весь срок процесса и переподключается сама. Недоступность брокера НЕ
    роняет шлюз: открытые сокеты остаются открытыми (они просто молчат),
    ``/health/ready`` показывает ``degraded``, а клиент по этому признаку уходит
    догонять ленту.
    """

    def __init__(self, hub: ConnectionHub) -> None:
        self._hub = hub
        self._task: asyncio.Task | None = None
        self._connected = False
        self._delivered = 0
        self._dropped = 0

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def stats(self) -> dict[str, int]:
        return {'delivered': self._delivered, 'undeliverable': self._dropped}

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name='notifications-ws-consumer')

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        self._connected = False

    async def _run(self) -> None:
        while True:
            try:
                await self._consume()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — брокер может отвалиться как угодно
                self._connected = False
                logger.warning(
                    'Broker connection lost, retrying in %ss: %s', settings.NOTIFY_WS_AMQP_RECONNECT_DELAY, exc
                )
                await asyncio.sleep(settings.NOTIFY_WS_AMQP_RECONNECT_DELAY)

    async def _consume(self) -> None:
        connection = await aio_pika.connect_robust(settings.NOTIFY_AMQP_URL)
        async with connection:
            channel = await connection.channel()
            exchange = await channel.declare_exchange(
                settings.NOTIFY_WS_EXCHANGE, aio_pika.ExchangeType.FANOUT, durable=True
            )
            queue = await channel.declare_queue(exclusive=True, auto_delete=True, durable=False)
            await queue.bind(exchange)
            self._connected = True
            logger.info('Subscribed to %s as %s', settings.NOTIFY_WS_EXCHANGE, queue.name)

            async with queue.iterator(no_ack=True) as messages:
                async for message in messages:
                    self._dispatch(message.body)

    def _dispatch(self, body: bytes) -> None:
        try:
            push = NotificationPush.parse(json.loads(body))
        except (PushError, ValueError) as exc:
            # Отбрасываем и продолжаем: одно кривое сообщение не повод обрывать
            # соединения всех подключённых.
            logger.warning('Dropping malformed push: %s', exc)
            return
        receivers = self._hub.publish(push.user_id, push.to_frame())
        if receivers:
            self._delivered += 1
        else:
            # Не ошибка: адресат сейчас не онлайн, и его копия уже лежит в ленте
            # кабинета. Счётчик существует, чтобы «шлюз молчит» отличалось от
            # «до шлюза ничего не доезжает».
            self._dropped += 1
