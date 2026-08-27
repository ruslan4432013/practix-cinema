"""Порядок причин пропуска: причина, а не симптом.

Формирующий воркер оставляет адрес пустым для всего, кроме email и websocket
(телефона у нас нет, токена браузера тоже). Поэтому проверка адреса, стоящая
раньше проверки канала, всегда выигрывала бы гонку и записывала бы в журнал
«нет адреса» там, где на самом деле «канал не реализован» — а менеджер по такому
журналу пошёл бы искать несуществующую проблему с контактами.

У РЕАЛИЗОВАННЫХ каналов порядок обратный по смыслу: до проверки адреса дело
доходит, и пустой адрес там означает ровно то, что написано.
"""

import uuid

import pytest

from practix_notifications.campaigns.models import Campaign, DeliveryTask, ScheduledRun
from practix_notifications.enums import Category, Channel, SkipReason
from practix_notifications.services.delivery import _skip_reason
from practix_notifications.subscribers.models import ChannelOptout, Subscriber

pytestmark = pytest.mark.django_db


def _task(campaign: Campaign, subscriber: Subscriber, *, channel: str, address: str) -> DeliveryTask:
    run = ScheduledRun.objects.create(campaign=campaign, run_key=str(uuid.uuid4()), planned_for='2026-08-05T10:00:00Z')
    return DeliveryTask.objects.create(
        run=run,
        campaign=campaign,
        subscriber=subscriber,
        channel=channel,
        address=address,
        idempotency_key=str(uuid.uuid4()),
    )


@pytest.mark.parametrize('channel', [Channel.SMS.value, Channel.PUSH.value])
def test_unimplemented_channel_wins_over_empty_address(campaign, subscriber, channel):
    task = _task(campaign, subscriber, channel=channel, address='')
    assert _skip_reason(task, Category.MARKETING.value) == SkipReason.CHANNEL_UNAVAILABLE


def test_websocket_recipient_is_addressed_by_subscriber_id(campaign, subscriber):
    """Адрес websocket-получателя — его же идентификатор, и это НЕ «нет адреса».

    Раньше websocket стоял в списке нереализованных, и любая задача этого канала
    отсеивалась раньше проверки адреса. Теперь канал настоящий, поэтому проверка
    адреса до него доходит — и обязана его принять.
    """
    task = _task(campaign, subscriber, channel=Channel.WEBSOCKET.value, address=str(subscriber.id))
    assert _skip_reason(task, Category.MARKETING.value) is None


def test_websocket_without_address_is_no_address(campaign, subscriber):
    """Пустой адрес у РЕАЛИЗОВАННОГО канала — уже не симптом, а причина:
    получателя не удалось опознать, и в журнале должно стоять именно это."""
    task = _task(campaign, subscriber, channel=Channel.WEBSOCKET.value, address='')
    assert _skip_reason(task, Category.MARKETING.value) == SkipReason.NO_ADDRESS


def test_email_without_address_is_still_no_address(campaign, subscriber):
    subscriber.email = ''
    subscriber.save(update_fields=['email'])
    task = _task(campaign, subscriber, channel=Channel.EMAIL.value, address='')
    assert _skip_reason(task, Category.MARKETING.value) == SkipReason.NO_ADDRESS


def test_inactive_subscriber_wins_over_everything(campaign, subscriber):
    """Неактивный — это про человека, а не про канал: причина точнее."""
    subscriber.is_active = False
    subscriber.save(update_fields=['is_active'])
    task = _task(campaign, subscriber, channel=Channel.SMS.value, address='')
    assert _skip_reason(task, Category.MARKETING.value) == SkipReason.INACTIVE


def test_deliverable_email_is_not_skipped(campaign, subscriber):
    task = _task(campaign, subscriber, channel=Channel.EMAIL.value, address='ivan@example.com')
    assert _skip_reason(task, Category.MARKETING.value) is None


def test_optout_is_checked_after_the_channel(campaign, subscriber):
    """Отписка от нереализованного канала не должна маскировать причину:
    сообщение всё равно не ушло бы, и в журнале должно стоять почему."""
    ChannelOptout.objects.create(subscriber=subscriber, channel=Channel.SMS.value)
    task = _task(campaign, subscriber, channel=Channel.SMS.value, address='')
    assert _skip_reason(task, Category.MARKETING.value) == SkipReason.CHANNEL_UNAVAILABLE


def test_optout_still_applies_to_email(campaign, subscriber):
    ChannelOptout.objects.create(subscriber=subscriber, channel=Channel.EMAIL.value)
    task = _task(campaign, subscriber, channel=Channel.EMAIL.value, address='ivan@example.com')
    assert _skip_reason(task, Category.MARKETING.value) == SkipReason.OPTED_OUT


def test_transactional_letters_ignore_the_optout(campaign, subscriber):
    ChannelOptout.objects.create(subscriber=subscriber, channel=Channel.EMAIL.value)
    task = _task(campaign, subscriber, channel=Channel.EMAIL.value, address='ivan@example.com')
    assert _skip_reason(task, Category.TRANSACTIONAL.value) is None
