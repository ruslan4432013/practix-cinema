"""Лента кабинета: одна строка на доставку, превью вместо тела письма."""

import uuid
from datetime import UTC, datetime

import pytest

from practix_notifications.campaigns.models import Campaign, DeliveryTask, ScheduledRun
from practix_notifications.channels import RenderedMessage
from practix_notifications.core.config import settings
from practix_notifications.enums import Category
from practix_notifications.inbox import services as inbox
from practix_notifications.inbox.models import InboxMessage
from practix_notifications.subscribers.models import Subscriber

pytestmark = pytest.mark.django_db

SENT_AT = datetime(2026, 8, 5, 10, 0, tzinfo=UTC)


@pytest.fixture
def task(campaign: Campaign, subscriber: Subscriber) -> DeliveryTask:
    Campaign.objects.filter(pk=campaign.pk).update(category=Category.DIGEST.value)
    campaign.refresh_from_db()
    run = ScheduledRun.objects.create(
        campaign=campaign, run_key='evt:film.published:1', planned_for=SENT_AT, event_type='film.published'
    )
    return DeliveryTask.objects.create(
        run=run,
        campaign=campaign,
        subscriber=subscriber,
        channel='email',
        address='ivan@example.com',
        idempotency_key=str(uuid.uuid4()),
    )


def _record(task: DeliveryTask, message: RenderedMessage) -> None:
    inbox.record(task=task, run=task.run, message=message, sent_at=SENT_AT)


def test_record_writes_one_row(task):
    _record(task, RenderedMessage(subject='Новинка', body='<p>Дюна</p>'))

    message = InboxMessage.objects.get()
    assert message.subject == 'Новинка'
    assert message.subscriber_id == task.subscriber_id
    assert message.category == Category.DIGEST.value
    assert message.event_type == 'film.published'
    assert message.sent_at == SENT_AT
    assert message.read_at is None


def test_second_record_on_the_same_task_is_a_no_op(task):
    """Повторная доставка пачки не должна давать вторую строку ленты."""
    _record(task, RenderedMessage(subject='Первая', body='<p>a</p>'))
    _record(task, RenderedMessage(subject='Вторая', body='<p>b</p>'))

    assert InboxMessage.objects.count() == 1
    assert InboxMessage.objects.get().subject == 'Первая'


def test_preview_strips_tags_and_collapses_whitespace(task):
    _record(task, RenderedMessage(subject='Тема', body='<h1>Дюна</h1>\n\n  <p>Уже   в  каталоге</p>'))

    assert InboxMessage.objects.get().preview == 'Дюна Уже в каталоге'


def test_preview_is_truncated_and_the_body_is_not_stored(task, monkeypatch):
    monkeypatch.setattr(settings, 'NOTIFY_INBOX_PREVIEW_CHARS', 64)
    long_body = '<p>' + ('текст ' * 500) + '</p>'

    _record(task, RenderedMessage(subject='Тема', body=long_body))

    message = InboxMessage.objects.get()
    assert len(message.preview) == 64
    # Полного тела в ленте нет: сто тысяч получателей — это сто тысяч копий
    # одного HTML, а письмо целиком лежит в почте пользователя.
    assert not hasattr(message, 'body')


def test_plain_text_body_is_kept_as_is(task):
    _record(task, RenderedMessage(subject='Тема', body='Просто текст', is_html=False))

    assert InboxMessage.objects.get().preview == 'Просто текст'


def test_disabled_inbox_writes_nothing(task, monkeypatch):
    monkeypatch.setattr(settings, 'NOTIFY_INBOX_ENABLED', False)

    _record(task, RenderedMessage(subject='Тема', body='<p>Тело</p>'))

    assert InboxMessage.objects.count() == 0


def test_purge_removes_only_the_old(task):
    _record(task, RenderedMessage(subject='Старое', body='<p>a</p>'))
    InboxMessage.objects.update(sent_at=datetime(2020, 1, 1, tzinfo=UTC))

    assert inbox.purge(older_than_days=180, now=SENT_AT) == 1
    assert InboxMessage.objects.count() == 0


def test_purge_keeps_the_recent(task):
    _record(task, RenderedMessage(subject='Свежее', body='<p>a</p>'))

    assert inbox.purge(older_than_days=180, now=SENT_AT) == 0
    assert InboxMessage.objects.count() == 1
