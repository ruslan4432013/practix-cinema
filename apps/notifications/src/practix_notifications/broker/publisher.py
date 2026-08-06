"""Соединение с брокером и публикация сообщений.

Требования из теории, каждое — строкой кода:

* ``durable`` очереди (см. ``topology``);
* ``delivery_mode=2`` — сообщение пишется на диск, а не живёт в памяти;
* publisher confirms — не идём дальше, пока сервер не подтвердил приём;
* ``X-Request-Id`` в заголовках, «потом по логам будет проще понять, где берёт
  начало та или иная проблема».

``mandatory=True`` добавлен сверх списка: без него сообщение с ключом
маршрутизации, который никуда не привязан, молча исчезает — брокер принимает его
и выбрасывает. С ``mandatory`` pika поднимет ``UnroutableError``, публикация не
будет отмечена успешной, и запись останется в outbox до следующей попытки.
"""

import json
import logging
from typing import Any

import pika
from pika.exceptions import AMQPError, UnroutableError

from practix_core.context import get_request_id
from practix_notifications.broker import topology
from practix_notifications.broker.envelope import SCHEMA_VERSION
from practix_notifications.core.config import settings

logger = logging.getLogger('notifications.broker')


class PublishFailed(Exception):
    """Брокер не подтвердил приём сообщения."""


class BrokerConnection:
    """Тонкая обёртка над ``BlockingConnection`` с ленивым переподключением.

    Синхронная (``BlockingConnection``), а не асинхронная: и воркер, и
    планировщик — синхронные процессы, а канал pika не потокобезопасен. Попытка
    развести их по потокам — классический способ получить
    ``ChannelWrongStateError`` под нагрузкой.
    """

    def __init__(self, url: str | None = None) -> None:
        self._url = url or settings.NOTIFY_AMQP_URL
        self._connection: pika.BlockingConnection | None = None
        self._channel: Any = None

    # --- жизненный цикл ---------------------------------------------------

    @property
    def channel(self) -> Any:
        if self._channel is None or self._channel.is_closed:
            self._connect()
        return self._channel

    def _connect(self) -> None:
        parameters = pika.URLParameters(self._url)
        # Heartbeat заметно больше самой долгой блокирующей операции воркера: пока
        # smtplib ждёт почтовый сервер, pika не обслуживает соединение, и короткий
        # heartbeat уронил бы канал ровно в момент отправки, переотправив пачку.
        parameters.heartbeat = settings.NOTIFY_AMQP_HEARTBEAT
        parameters.blocked_connection_timeout = settings.NOTIFY_AMQP_BLOCKED_TIMEOUT
        self._connection = pika.BlockingConnection(parameters)
        self._channel = self._connection.channel()
        # Подтверждения включаются один раз на канал.
        self._channel.confirm_delivery()
        topology.declare_topology(
            self._channel,
            retry_ttl_ms=settings.NOTIFY_RETRY_TTL_MS,
            build_retry_ttl_ms=settings.NOTIFY_RETRY_BUILD_TTL_MS,
        )
        logger.info('Connected to broker', extra={'amqp_url': _mask(self._url)})

    def close(self) -> None:
        try:
            if self._connection is not None and self._connection.is_open:
                self._connection.close()
        except AMQPError:
            logger.warning('Broker connection did not close cleanly', exc_info=True)
        finally:
            self._connection = None
            self._channel = None

    def process_events(self, seconds: float = 0) -> None:
        """Дать pika обслужить heartbeat, не отдавая ему управление насовсем.

        Нужно между отправками писем: ``smtplib`` блокирует поток, и без этого
        вызова соединение с брокером считается мёртвым посреди длинной пачки.
        """
        if self._connection is not None and self._connection.is_open:
            self._connection.process_data_events(time_limit=seconds)

    # --- публикация -------------------------------------------------------

    def publish(
        self,
        *,
        exchange: str,
        routing_key: str,
        body: dict[str, Any],
        headers: dict[str, Any] | None = None,
        mandatory: bool = True,
        persistent: bool = True,
        expiration_ms: int | None = None,
    ) -> None:
        """Опубликовать сообщение.

        ``mandatory`` и ``persistent`` по умолчанию включены — это правильный
        режим для всего, что обязано быть обработано. Выключаются они ровно в
        одном месте, у push'ей websocket-шлюза, и там ОБА выключения
        обязательны:

        * у fanout-обменника ``notifications.ws`` не бывает привязанных очередей,
          пока не поднят ни один шлюз (профильный контейнер, его может не быть
          вовсе). С ``mandatory=True`` каждое websocket-уведомление получало бы
          ``UnroutableError`` → ``PublishFailed`` → бесконечный ярус повторов;
        * push, который некому показать, через минуту бесполезен, и писать его
          на диск незачем: долговечная копия — строка в ``inbox_message``.

        ``expiration_ms`` ставит срок жизни НА СООБЩЕНИИ. Нужен ровно там, где
        очередь никто не разгребает и ограничить её аргументами уже нельзя, —
        у отчётных событий. Обычная опасность такого TTL (очередь протухает
        только с головы, и долгоживущее сообщение задержит стоящие за ним) там
        не возникает: срок у всех отчётов одинаков.
        """
        properties = pika.BasicProperties(
            content_type='application/json',
            content_encoding='utf-8',
            # 2 = persistent: сервер обязан положить сообщение на диск.
            delivery_mode=2 if persistent else 1,
            # RabbitMQ ждёт СТРОКУ с числом миллисекунд; число молча игнорируется.
            expiration=str(expiration_ms) if expiration_ms else None,
            message_id=str(body.get('event_id') or ''),
            correlation_id=str(body.get('notification_id') or ''),
            headers=self._headers(headers),
        )
        try:
            self.channel.basic_publish(
                exchange=exchange,
                routing_key=routing_key,
                body=json.dumps(body, ensure_ascii=False).encode('utf-8'),
                properties=properties,
                mandatory=mandatory,
            )
        except UnroutableError as exc:
            raise PublishFailed(f'Нет привязки под ключ {routing_key!r}: сообщение некуда доставить') from exc
        except AMQPError as exc:
            # Канал после ошибки протокола непригоден — уронить его, чтобы
            # следующий вызов переподключился.
            self.close()
            raise PublishFailed(f'Брокер не подтвердил публикацию: {exc}') from exc

    def republish_to_retry(
        self, *, body: dict[str, Any], headers: dict[str, Any], attempt: int, routing_key: str
    ) -> None:
        """Отправить сообщение на парковку с увеличенным счётчиком попыток.

        ``routing_key`` обязателен и без значения по умолчанию. Ярусов парковки
        два, и ключ определяет, в какую рабочую очередь сообщение вернётся:
        подставленный «по умолчанию» ключ отправил бы отложенное письмо на
        повторную сборку, то есть на лишний поход в Auth за уже известными
        данными.
        """
        self.publish(
            exchange=topology.EXCHANGE_RETRY,
            routing_key=routing_key,
            body=body,
            headers={**headers, topology.HEADER_ATTEMPT: attempt},
        )

    def _headers(self, extra: dict[str, Any] | None) -> dict[str, Any]:
        headers: dict[str, Any] = {topology.HEADER_SCHEMA_VERSION: SCHEMA_VERSION}
        request_id = get_request_id()
        if request_id:
            headers[topology.HEADER_REQUEST_ID] = request_id
        if extra:
            headers.update({key: value for key, value in extra.items() if value is not None})
        return headers


def _mask(url: str) -> str:
    """Убрать пароль из строки подключения перед записью в лог."""
    if '@' not in url:
        return url
    scheme, _, rest = url.partition('://')
    _, _, host = rest.rpartition('@')
    return f'{scheme}://***@{host}'
