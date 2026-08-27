"""Топология RabbitMQ: точки обмена, очереди и привязки.

Имена подчинены двум конвенциям из теории («Брокер сообщений RabbitMQ»):

* ключ маршрутизации — ``[сущность]-reporting.[версия].[событие]``;
* очередь — ``[микросервис].[действие-консьюмера]``, чтобы по имени в алерте
  было понятно, кто её слушает и за что отвечает.

## Два шага на пути письма

``notifications.build-email``
    слушает формирующий воркер. Приходит пачка идентификаторов, он ходит в Auth
    за именем и адресом, рендерит письмо и публикует его дальше.
``notifications.send-email``
    слушает отправляющий воркер. Приходит готовое письмо — остаётся SMTP.

Почему два, а не один: коннект к SMTP стоит около пяти секунд, поэтому
отправляющий воркер держит соединение открытым и не должен простаивать в нём,
ожидая HTTP-ответ Auth. Плюс воркеры упираются в разные узкие места (Auth и
почтовый сервер) и масштабируются независимо, а при отказе Auth можно погасить
только формирующие — ровно тот failover, которого требует чек-лист теории.

## Как устроен повтор

Три точки обмена, а не одна:

``notifications.events``
    рабочая. Всё, что должно быть обработано.
``notifications.retry``
    парковка для ВРЕМЕННЫХ отказов. Очереди ``notifications.retry-build-email``
    и ``notifications.retry-send-email`` не имеют консьюмера вообще: сообщение
    лежит в них ``x-message-ttl``, протухает и по ``x-dead-letter-exchange``
    возвращается обратно в рабочую точку обмена — каждое со СВОИМ
    ``x-dead-letter-routing-key``, то есть в ту же очередь, откуда пришло.
``notifications.dlx``
    терминальные отказы: сообщение, которое исчерпало попытки или которое
    невозможно разобрать.

Ярусов повтора два, потому что они ждут разного: отправка ждёт почтовый сервер
(секунды), сборка — целую подсистему пользователей (минуты).

Почему не ``basic_nack(requeue=True)``: он возвращает сообщение в голову той же
очереди немедленно, и при устойчивом отказе (почтовый сервер лежит) получается
горячий цикл, который выжигает CPU и забивает лог.

Почему TTL постоянный, а не на сообщение: очередь в RabbitMQ протухает только с
головы. Сообщение с TTL 10 минут, легшее первым, задержит стоящее за ним с TTL
10 секунд на все 10 минут. Нужен экспоненциальный backoff — заводятся ЯРУСЫ
очередей с разными постоянными TTL, а не TTL на сообщение.

## Ловушка обновления стенда

Очередь ``notifications.retry-email`` из прошлой версии здесь БОЛЬШЕ НЕ
объявляется, и это осознанно: переобъявить существующую очередь с другим
``x-dead-letter-routing-key`` — это ``PRECONDITION_FAILED`` (406), закрытый
канал и краш-цикл всех воркеров. На живом стенде её нужно удалить руками (или
поднять стенд с ``down -v``); объявление, исчезнувшее из кода, само с сервера
ничего не убирает.
"""

import pika

EXCHANGE_EVENTS = 'notifications.events'
EXCHANGE_RETRY = 'notifications.retry'
EXCHANGE_DLX = 'notifications.dlx'

#: Раздача push'ей websocket-шлюзу. Единственная точка обмена типа ``fanout``:
#: соединение живёт в конкретном процессе конкретной реплики шлюза, и push
#: обязан доехать до ВСЕХ реплик — каждая сама решит, держит ли она адресата.
#: Topic с ключом по пользователю не подошёл бы: очередь на пользователя — это
#: очередь на каждого, кто когда-либо открывал вкладку.
#:
#: Объявление здесь и в шлюзе обязано совпадать (``fanout``, ``durable=True``):
#: расхождение по типу или durability даёт PRECONDITION_FAILED (406) и краш-цикл
#: у того, кто объявил вторым, — тот же класс ловушки, что с `retry-email` ниже.
EXCHANGE_WS = 'notifications.ws'

RK_CAMPAIGN_LAUNCHED = 'campaign-reporting.v1.launched'
RK_NOTIFICATION_REQUESTED = 'notification-reporting.v1.requested'
RK_NOTIFICATION_PREPARED = 'notification-reporting.v1.prepared'
RK_NOTIFICATION_DELIVERED = 'notification-reporting.v1.delivered'
RK_NOTIFICATION_FAILED = 'notification-reporting.v1.failed'

QUEUE_FANOUT = 'notifications.fan-out-campaign'
QUEUE_BUILD_EMAIL = 'notifications.build-email'
QUEUE_SEND_EMAIL = 'notifications.send-email'
QUEUE_RETRY_BUILD = 'notifications.retry-build-email'
QUEUE_RETRY_SEND = 'notifications.retry-send-email'
QUEUE_DEAD_LETTERS = 'notifications.dead-letters'
QUEUE_REPORTS = 'notifications.record-delivery'

#: Привязка очереди отправки к `.requested` из прошлой версии. Объявлять её
#: больше нельзя, а СНЯТЬ нужно явно: `queue_bind` аддитивен, забытая привязка
#: пережила бы обновление, и отправляющий воркер продолжил бы получать сырые
#: пачки, в которых для него нет ни одного готового письма.
LEGACY_SEND_BINDING = RK_NOTIFICATION_REQUESTED

#: Заголовок счётчика попыток. Живёт в headers, а не в теле: тело — контракт с
#: потребителями, а счётчик попыток — деталь транспорта.
HEADER_ATTEMPT = 'x-attempt'
HEADER_REQUEST_ID = 'X-Request-Id'
HEADER_SCHEMA_VERSION = 'x-schema-version'


def declare_topology(
    channel: 'pika.adapters.blocking_connection.BlockingChannel',
    *,
    retry_ttl_ms: int,
    build_retry_ttl_ms: int,
) -> None:
    """Идемпотентно объявляет всю топологию.

    Вызывается на старте воркера и планировщика, а не разовой миграцией: очередь,
    удалённая руками в UI, должна восстанавливаться перезапуском контейнера, а не
    походом в документацию.

    ВСЁ ОБЪЯВЛЯЕТСЯ ``durable``. Иначе перезагрузка сервера очередей уносит и
    очереди, и всё, что в них лежало.
    """
    for exchange in (EXCHANGE_EVENTS, EXCHANGE_RETRY, EXCHANGE_DLX):
        channel.exchange_declare(exchange=exchange, exchange_type='topic', durable=True)

    # Очередей под ним НЕ объявляется, и это не забывчивость: их заводит сам
    # шлюз — exclusive, auto-delete, по одной на процесс. Пока ни одного шлюза
    # нет, push'и уходят в никуда, и это правильный исход: долговечная копия
    # уведомления лежит в inbox_message, а показывать сообщение некому.
    channel.exchange_declare(exchange=EXCHANGE_WS, exchange_type='fanout', durable=True)

    dead_letter_args = {'x-dead-letter-exchange': EXCHANGE_DLX}

    channel.queue_declare(queue=QUEUE_FANOUT, durable=True, arguments=dead_letter_args)
    channel.queue_bind(queue=QUEUE_FANOUT, exchange=EXCHANGE_EVENTS, routing_key=RK_CAMPAIGN_LAUNCHED)

    channel.queue_declare(queue=QUEUE_BUILD_EMAIL, durable=True, arguments=dead_letter_args)
    channel.queue_bind(queue=QUEUE_BUILD_EMAIL, exchange=EXCHANGE_EVENTS, routing_key=RK_NOTIFICATION_REQUESTED)

    channel.queue_declare(queue=QUEUE_SEND_EMAIL, durable=True, arguments=dead_letter_args)
    channel.queue_bind(queue=QUEUE_SEND_EMAIL, exchange=EXCHANGE_EVENTS, routing_key=RK_NOTIFICATION_PREPARED)
    # Снятие привязки предыдущей версии — см. LEGACY_SEND_BINDING. На чистом
    # стенде это но-оп, на обновлённом — единственное, что не даёт отправляющему
    # воркеру получать пачки, предназначенные формирующему.
    channel.queue_unbind(queue=QUEUE_SEND_EMAIL, exchange=EXCHANGE_EVENTS, routing_key=LEGACY_SEND_BINDING)

    # Очереди без консьюмера. Их работа — подождать и вернуть сообщение обратно
    # ровно в ту очередь, из которой оно приехало.
    channel.queue_declare(
        queue=QUEUE_RETRY_BUILD,
        durable=True,
        arguments={
            'x-message-ttl': build_retry_ttl_ms,
            'x-dead-letter-exchange': EXCHANGE_EVENTS,
            'x-dead-letter-routing-key': RK_NOTIFICATION_REQUESTED,
        },
    )
    channel.queue_bind(queue=QUEUE_RETRY_BUILD, exchange=EXCHANGE_RETRY, routing_key=RK_NOTIFICATION_REQUESTED)

    channel.queue_declare(
        queue=QUEUE_RETRY_SEND,
        durable=True,
        arguments={
            'x-message-ttl': retry_ttl_ms,
            'x-dead-letter-exchange': EXCHANGE_EVENTS,
            'x-dead-letter-routing-key': RK_NOTIFICATION_PREPARED,
        },
    )
    channel.queue_bind(queue=QUEUE_RETRY_SEND, exchange=EXCHANGE_RETRY, routing_key=RK_NOTIFICATION_PREPARED)

    channel.queue_declare(queue=QUEUE_DEAD_LETTERS, durable=True)
    channel.queue_bind(queue=QUEUE_DEAD_LETTERS, exchange=EXCHANGE_DLX, routing_key='#')

    # Отчётные события: слушать их никто не обязан, но очередь объявлена, чтобы
    # сообщения не терялись безадресно и их было где посмотреть.
    #
    # Привязки перечислены поимённо, а НЕ маской `notification-reporting.v1.*`:
    # под маску попали бы и `.requested`, и `.prepared`, то есть каждая рабочая
    # пачка легла бы второй копией в очередь, которую никто не разгребает. Так
    # растёт очередь, про которую потом приходит алерт «переполнение» без единой
    # догадки почему.
    #
    # Аргументов у очереди НЕТ, и срок жизни отчётов задаётся НА СООБЩЕНИИ
    # (`expiration` в publisher, ключ `NOTIFY_REPORT_TTL_MS`). Очередь, которую
    # никто не разгребает, обязана быть ограничена — иначе она растёт всё время
    # работы сервиса и однажды упирается в диск брокера, унося с собой рабочие
    # очереди. Но `x-message-ttl` здесь поставить нельзя: очередь уже существует
    # на всех работающих стендах, а переобъявление с новыми аргументами — это
    # PRECONDITION_FAILED (406) и краш-цикл воркеров (см. ловушку с
    # `notifications.retry-email` выше).
    #
    # Обычный довод против TTL на сообщении — «очередь протухает только с
    # головы, и долгоживущее сообщение задержит стоящие за ним» — здесь не
    # работает: TTL у всех отчётов ОДИНАКОВ, поэтому порядок истечения совпадает
    # с порядком публикации и голова всегда протухает первой.
    channel.queue_declare(queue=QUEUE_REPORTS, durable=True)
    for routing_key in (RK_NOTIFICATION_DELIVERED, RK_NOTIFICATION_FAILED):
        channel.queue_bind(queue=QUEUE_REPORTS, exchange=EXCHANGE_EVENTS, routing_key=routing_key)
