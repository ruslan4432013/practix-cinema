"""Запись и чтение ленты уведомлений пользователя.

Лента — проекция журнала доставки, и пишется она в момент, когда задача
переходит в «отправлено», в ТОЙ ЖЕ транзакции. Момент выбран так не из удобства:

* на вееру письма ещё нет — есть намерение. Строка, написанная там, показала бы
  человеку сообщение, от которого он отписался, протухшее событие и то, что
  тихие часы отложили до утра;
* на вееру нет и текста: ``bulk_create`` создаёт задачи пачкой, а рендер
  персонального письма происходит в воркере.

Тела письма здесь нет намеренно — только тема и превью. Рассылка на сто тысяч
человек положила бы в базу сто тысяч копий одного HTML; письмо целиком лежит в
почте пользователя, а кабинету нужен ответ на вопрос «о чём меня уведомляли».
"""

import logging
import re
from datetime import UTC, datetime, timedelta

from django.utils.html import strip_tags

from practix_notifications.campaigns.models import DeliveryTask, ScheduledRun
from practix_notifications.channels import RenderedMessage
from practix_notifications.core.config import settings
from practix_notifications.inbox.models import InboxMessage

logger = logging.getLogger('notifications.inbox')

_WHITESPACE = re.compile(r'\s+')


def build_preview(message: RenderedMessage, *, limit: int) -> str:
    """Первые строки письма без разметки.

    ``strip_tags`` до схлопывания пробелов, а не после: иначе переводы строк
    внутри тегов склеили бы слова соседних абзацев.
    """
    text = strip_tags(message.body) if message.is_html else message.body
    collapsed = _WHITESPACE.sub(' ', text).strip()
    return collapsed[:limit]


def record(*, task: DeliveryTask, run: ScheduledRun, message: RenderedMessage, sent_at: datetime) -> None:
    """Положить доставленное уведомление в ленту получателя.

    ``get_or_create`` поверх ``OneToOne``: повторная доставка пачки из брокера не
    должна давать вторую строку, даже если ранний выход по терминальному статусу
    задачи когда-нибудь сломают.
    """
    if not settings.NOTIFY_INBOX_ENABLED:
        return
    InboxMessage.objects.get_or_create(
        task=task,
        defaults={
            'subscriber_id': task.subscriber_id,
            'campaign_id': task.campaign_id,
            'channel': task.channel,
            'category': run.campaign.category,
            'subject': message.subject[:512],
            'preview': build_preview(message, limit=settings.NOTIFY_INBOX_PREVIEW_CHARS),
            'event_type': run.event_type,
            'content_id': run.campaign.content_id,
            'sent_at': sent_at,
        },
    )


def purge(*, older_than_days: int, now: datetime | None = None) -> int:
    """Удалить старые записи ленты. Возвращает число удалённых строк.

    Без срока хранения проекция растёт вечно, повторяя объём журнала доставки, —
    а кабинет показывает последние уведомления, а не всю историю.
    """
    threshold = (now or datetime.now(UTC)) - timedelta(days=older_than_days)
    deleted, _ = InboxMessage.objects.filter(sent_at__lt=threshold).delete()
    if deleted:
        logger.info('Purged %d inbox messages older than %s', deleted, threshold.isoformat())
    return deleted
