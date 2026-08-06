"""Транзакционный outbox: мост между базой и брокером.

## Зачем он вообще

Публиковать в брокер прямо из обработчика кнопки «Отправить» нельзя. Между
``COMMIT`` (рассылка получила статус «в очереди») и ``basic_publish`` есть окно, и
падение в этом окне даёт рассылку, которая по данным запущена, а по факту не
существует. Обратный порядок — сначала публикация, потом коммит — даёт то же
самое зеркально: пачка уходит в очередь для запуска, которого нет в базе.

Теория предлагает ровно это лекарство: «Если отправка сообщения не удалась,
сообщение лучше записать в какое-нибудь временное хранилище, а через некоторое
время попытаться переотправить заново». Разница лишь в том, что мы пишем в
хранилище ВСЕГДА и в той же транзакции, что и бизнес-данные, — тогда «не удалась»
перестаёт быть особым случаем.

Слив делает планировщик своим тиком. Дубль публикации при падении между
``basic_publish`` и сохранением ``published_at`` возможен и безвреден:
``ScheduledRun.run_key`` и ``DeliveryTask.idempotency_key`` не дадут ему
превратиться во второе письмо.
"""

import logging
from datetime import UTC, datetime
from typing import Any

from django.db import transaction

from practix_core.context import get_request_id
from practix_notifications.broker import topology
from practix_notifications.broker.publisher import BrokerConnection, PublishFailed
from practix_notifications.campaigns.models import OutboxMessage
from practix_notifications.core.config import settings

logger = logging.getLogger('notifications.outbox')


def enqueue(*, exchange: str, routing_key: str, body: dict[str, Any], headers: dict[str, Any] | None = None) -> None:
    """Положить сообщение в очередь на публикацию.

    Вызывается ВНУТРИ транзакции вместе с бизнес-данными — в этом весь смысл.
    """
    payload = dict(headers or {})
    request_id = get_request_id()
    if request_id:
        payload.setdefault(topology.HEADER_REQUEST_ID, request_id)
    OutboxMessage.objects.create(exchange=exchange, routing_key=routing_key, body=body, headers=payload)


def drain(broker: BrokerConnection, *, limit: int) -> int:
    """Опубликовать накопившееся. Возвращает число отправленных сообщений.

    ``skip_locked`` позволяет держать две реплики планировщика: вторая просто
    пройдёт мимо строк, которые уже забрала первая, вместо того чтобы ждать на
    блокировке.

    ## Почему у строки есть потолок попыток

    ``PublishFailed`` приходит и когда брокер недоступен (лечится ожиданием), и
    когда у ключа маршрутизации нет ни одной привязки (``UnroutableError``, не
    лечится ничем). Различить их по исключению нельзя, а выборка идёт по
    ``created_at``, поэтому вторая разновидность встаёт в голову и останавливает
    публикацию ВСЕХ рассылок навсегда. ``NOTIFY_OUTBOX_MAX_ATTEMPTS`` ограничивает
    это несколькими тиками: строка выпадает из выборки, остаётся в базе для
    разбора и попадает в ``/health/ready`` отдельным счётчиком.
    """
    sent = 0
    for _ in range(limit):
        # Блокировка держится ВСЁ ВРЕМЯ публикации, а не берётся и сразу
        # отпускается: иначе вторая реплика подхватила бы ту же строку, пока
        # первая ещё разговаривает с брокером, и сообщение ушло бы дважды.
        with transaction.atomic():
            message = (
                OutboxMessage.objects.select_for_update(skip_locked=True)
                .filter(published_at__isnull=True, attempts__lt=settings.NOTIFY_OUTBOX_MAX_ATTEMPTS)
                .order_by('created_at')
                .first()
            )
            if message is None:
                break
            try:
                broker.publish(
                    exchange=message.exchange,
                    routing_key=message.routing_key,
                    body=message.body,
                    headers=message.headers,
                )
            except PublishFailed as exc:
                message.attempts += 1
                message.last_error = str(exc)[:2000]
                message.save(update_fields=['attempts', 'last_error'])
                if message.attempts >= settings.NOTIFY_OUTBOX_MAX_ATTEMPTS:
                    logger.error(
                        'Outbox message %s giving up after %s attempts (%s → %s): %s',
                        message.id,
                        message.attempts,
                        message.exchange,
                        message.routing_key,
                        exc,
                    )
                else:
                    logger.warning('Outbox publish failed, will retry: %s', exc)
                # Дальше не идём: если брокер недоступен, следующие сообщения
                # упрутся в то же самое, а лог заполнится копиями одной ошибки.
                # На следующем тике эта строка либо пройдёт, либо исчерпает
                # потолок и перестанет загораживать очередь собой.
                return sent
            message.published_at = datetime.now(UTC)
            message.save(update_fields=['published_at'])
        sent += 1
    return sent
