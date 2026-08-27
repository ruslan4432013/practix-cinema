"""Потребление сообщений: общий цикл, повторы и отсев неразбираемых.

Обработчик возвращает решение, а не бросает исключения «на удачу»: три исхода
(``ACK`` / ``RETRY`` / ``DEAD``) различаются по последствиям, и путать их дорого.

``ACK``
    обработано. Подтверждаем.
``RETRY``
    временный отказ (почтовый сервер не отвечает, база флапает). Перепубликуем в
    парковочную очередь с увеличенным счётчиком и подтверждаем исходное — иначе
    оно вернулось бы в голову очереди и заблокировало всё за собой.
``DEAD``
    сообщение неразбираемо или исчерпало попытки. ``basic_nack(requeue=False)``
    отправляет его в dead-letters, где на него можно посмотреть глазами.

Отдельно про ``basic_qos(prefetch_count=1)``: воркер обрабатывает пачку
получателей минутами, и брокер не должен выдавать ему вторую, пока первая не
подтверждена, — иначе при падении воркера переотправится вдвое больше.
"""

import json
import logging
from collections.abc import Callable
from enum import Enum
from typing import Any

from pika.exceptions import AMQPError

from practix_core.context import request_id_ctx
from practix_notifications.broker import topology
from practix_notifications.broker.publisher import BrokerConnection, PublishFailed
from practix_notifications.core.config import settings

logger = logging.getLogger('notifications.consumer')


class Outcome(Enum):
    ACK = 'ack'
    RETRY = 'retry'
    DEAD = 'dead'


#: (тело, попытка) -> исход
Handler = Callable[[dict[str, Any], int], Outcome]


def handle_delivery(
    broker: BrokerConnection,
    *,
    channel: Any,
    method: Any,
    properties: Any,
    raw_body: bytes,
    handler: Handler,
    max_attempts: int | None = None,
) -> None:
    """Разобрать сообщение, отдать обработчику и решить его судьбу.

    Идентификатор запроса восстанавливается из заголовка в ContextVar: с этого
    момента каждая строка лога воркера подписана тем же ``request_id``, что и
    нажатие кнопки в админке, породившее рассылку.

    ``max_attempts`` — параметр, а не общая настройка: формирующий воркер ждёт
    подсистему пользователей (минуты), отправляющий — почтовый сервер (секунды),
    и бюджет повторов у них разный.
    """
    budget = max_attempts if max_attempts is not None else settings.NOTIFY_MAX_ATTEMPTS
    headers = dict(properties.headers or {})
    attempt = int(headers.get(topology.HEADER_ATTEMPT, 0))
    token = request_id_ctx.set(headers.get(topology.HEADER_REQUEST_ID))
    try:
        try:
            body = json.loads(raw_body.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            # Битое сообщение не чинится повтором — только в dead-letters.
            logger.exception('Message is not valid JSON, dead-lettering')
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return

        try:
            outcome = handler(body, attempt)
        # Неожиданная ошибка обработчика не должна ронять воркер: пачка уедет
        # в повтор, а трейсбек — в лог и в сборщик ошибок.
        except Exception:
            logger.exception('Unhandled error in message handler, scheduling retry')
            outcome = Outcome.RETRY

        if outcome is Outcome.ACK:
            channel.basic_ack(delivery_tag=method.delivery_tag)
            return

        if outcome is Outcome.DEAD or attempt + 1 >= budget:
            if outcome is not Outcome.DEAD:
                logger.error('Attempts exhausted (%s), dead-lettering', attempt + 1)
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return

        try:
            # Ключ маршрутизации — тот, с которым сообщение приехало: в точке
            # обмена повторов на каждый рабочий ключ своя очередь, поэтому
            # сообщение возвращается ровно туда, откуда пришло.
            broker.republish_to_retry(body=body, headers=headers, attempt=attempt + 1, routing_key=method.routing_key)
        except PublishFailed:
            # Парковка недоступна — пусть сообщение вернётся в очередь и приедет
            # снова: потерять его хуже, чем обработать повторно.
            logger.exception('Could not park message for retry, requeueing')
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
            return
        channel.basic_ack(delivery_tag=method.delivery_tag)
    finally:
        request_id_ctx.reset(token)


def consume_forever(
    broker: BrokerConnection,
    *,
    queue: str,
    handler: Handler,
    stop: Callable[[], bool],
    max_attempts: int | None = None,
) -> None:
    """Слушать очередь, пока ``stop()`` не вернёт True.

    Реализовано через ``consume`` с таймаутом, а не через ``basic_consume`` +
    ``start_consuming``: последний отдаёт управление pika насовсем, и корректно
    отреагировать на SIGTERM внутри него нечем.
    """
    channel = broker.channel
    channel.basic_qos(prefetch_count=settings.NOTIFY_PREFETCH_COUNT)
    logger.info('Consuming %s', queue)

    while not stop():
        try:
            for method, properties, body in channel.consume(queue, inactivity_timeout=1.0):
                if method is None:  # таймаут бездействия — шанс проверить stop()
                    if stop():
                        break
                    continue
                handle_delivery(
                    broker,
                    channel=channel,
                    method=method,
                    properties=properties,
                    raw_body=body,
                    handler=handler,
                    max_attempts=max_attempts,
                )
                if stop():
                    break
        except AMQPError:
            logger.exception('Broker connection lost, reconnecting')
            broker.close()
            if stop():
                break
            channel = broker.channel
            channel.basic_qos(prefetch_count=settings.NOTIFY_PREFETCH_COUNT)

    channel.cancel()


def drain_queue(
    broker: BrokerConnection, *, queue: str, handler: Handler, limit: int, max_attempts: int | None = None
) -> int:
    """Разобрать до ``limit`` сообщений и вернуть управление.

    Нужно планировщику: он в одном потоке и тикает по расписаниям, сливает outbox
    и разворачивает рассылки. Занять его ``consume_forever`` нельзя — тик
    перестал бы наступать.
    """
    channel = broker.channel
    processed = 0
    for _ in range(limit):
        method, properties, body = channel.basic_get(queue=queue, auto_ack=False)
        if method is None:
            break
        handle_delivery(
            broker,
            channel=channel,
            method=method,
            properties=properties,
            raw_body=body,
            handler=handler,
            max_attempts=max_attempts,
        )
        processed += 1
    return processed
