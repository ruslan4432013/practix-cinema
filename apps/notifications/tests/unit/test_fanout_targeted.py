"""Веер: адресный прогон отдаёт одного, обычный — прежнюю аудиторию.

Второе важнее первого: правка веера обязана быть поведенчески нейтральной для
всех существующих путей, иначе адресные письма куплены ценой массовых.
"""

import uuid

import pytest

from practix_notifications.campaigns.models import Campaign, ScheduledRun
from practix_notifications.enums import Channel, SegmentKind
from practix_notifications.services.fanout import _iter_subscribers
from practix_notifications.subscribers.models import Segment, SegmentMember, Subscriber

pytestmark = pytest.mark.django_db


def _run(campaign: Campaign, **kwargs) -> ScheduledRun:
    return ScheduledRun.objects.create(
        campaign=campaign, run_key=str(uuid.uuid4()), planned_for='2026-08-05T10:00:00Z', **kwargs
    )


def _subscriber(login: str, email: str, **kwargs) -> Subscriber:
    return Subscriber.objects.create(id=uuid.uuid4(), login=login, email=email, **kwargs)


def _flat(run: ScheduledRun, channel: str = Channel.EMAIL.value) -> list[Subscriber]:
    return [subscriber for chunk in _iter_subscribers(run, channel) for subscriber in chunk]


def test_targeted_run_yields_exactly_one(campaign):
    target = _subscriber('ivan', 'ivan@example.com')
    _subscriber('petr', 'petr@example.com')

    assert _flat(_run(campaign, audience_subscriber=target)) == [target]


def test_targeted_run_yields_a_subscriber_without_an_address(campaign):
    """Для массовой рассылки безадресный подписчик — шум, а для письма
    конкретному человеку это факт, который должен попасть в журнал как no_address."""
    target = _subscriber('ivan', '')

    assert _flat(_run(campaign, audience_subscriber=target)) == [target]


def test_targeted_run_yields_an_inactive_subscriber_too(campaign):
    """Причину пропуска называет доставка, а не веер: 'inactive' в журнале
    информативнее, чем беззвучно пустой прогон."""
    target = _subscriber('ivan', 'ivan@example.com', is_active=False)

    assert _flat(_run(campaign, audience_subscriber=target)) == [target]


def test_deleting_the_subscriber_removes_the_targeted_run(campaign):
    """Прогон не может указывать на несуществующего человека: CASCADE уносит его
    вместе с подписчиком. Именно поэтому в вееру нет ветки «получатель пропал»."""
    target = _subscriber('ivan', 'ivan@example.com')
    run = _run(campaign, audience_subscriber=target)

    target.delete()

    assert not ScheduledRun.objects.filter(pk=run.pk).exists()


def test_mass_run_still_skips_the_inactive_and_the_addressless(campaign):
    active = _subscriber('ivan', 'ivan@example.com')
    _subscriber('petr', 'petr@example.com', is_active=False)
    _subscriber('anna', '')

    assert _flat(_run(campaign)) == [active]


def test_mass_websocket_run_keeps_subscribers_without_email(campaign):
    """Отсев безадресных зависит от КАНАЛА.

    Для websocket почты не существует как понятия — адресом служит идентификатор
    подписчика, — и человек без email обязан попасть в аудиторию. Неактивный
    по-прежнему не попадает: это про человека, а не про канал.
    """
    with_email = _subscriber('ivan', 'ivan@example.com')
    without_email = _subscriber('anna', '')
    _subscriber('petr', 'petr@example.com', is_active=False)

    # Множеством, а не списком: курсор идёт по id, а они случайные UUID.
    assert set(_flat(_run(campaign), Channel.WEBSOCKET.value)) == {with_email, without_email}


def test_mass_run_still_applies_the_segment(campaign):
    inside = _subscriber('ivan', 'ivan@example.com')
    _subscriber('petr', 'petr@example.com')
    segment = Segment.objects.create(code='vip', name='VIP', kind=SegmentKind.STATIC.value)
    SegmentMember.objects.create(segment=segment, subscriber=inside)
    Campaign.objects.filter(pk=campaign.pk).update(segment=segment)

    assert _flat(_run(Campaign.objects.get(pk=campaign.pk))) == [inside]


def test_mass_run_pages_by_cursor(campaign, monkeypatch):
    from practix_notifications.core.config import settings

    monkeypatch.setattr(settings, 'NOTIFY_FANOUT_DB_BATCH', 2)
    for index in range(5):
        _subscriber(f'user{index}', f'user{index}@example.com')

    chunks = list(_iter_subscribers(_run(campaign), Channel.EMAIL.value))

    assert [len(chunk) for chunk in chunks] == [2, 2, 1]
    ids = [subscriber.id for chunk in chunks for subscriber in chunk]
    assert ids == sorted(ids), 'курсор идёт по возрастанию id, иначе пачки перекрывались бы'


class TestFilterSegment:
    """Сегмент-фильтр: неизвестный ключ означает пустую аудиторию, а не всю базу.

    Раньше ключи вне белого списка молча выбрасывались. Первая же опечатка
    менеджера — ``locale__in`` вместо ``locale`` — сокращала условие до пустого
    словаря, то есть до ``filter()``, то есть до рассылки на ВСЮ активную базу
    вместо горстки людей. Пустая аудитория — заметная ошибка, лишнее письмо ста
    тысячам человек — неисправимая.
    """

    def _segment(self, campaign: Campaign, criteria: dict) -> ScheduledRun:
        segment = Segment.objects.create(
            code='by-locale', name='По языку', kind=SegmentKind.FILTER.value, filter=criteria
        )
        Campaign.objects.filter(pk=campaign.pk).update(segment=segment)
        return _run(Campaign.objects.get(pk=campaign.pk))

    def test_whitelisted_field_still_works(self, campaign):
        ru = _subscriber('ivan', 'ivan@example.com', locale='ru')
        _subscriber('john', 'john@example.com', locale='en')

        assert _flat(self._segment(campaign, {'locale': 'ru'})) == [ru]

    def test_unknown_field_yields_nobody(self, campaign):
        _subscriber('ivan', 'ivan@example.com', locale='ru')
        _subscriber('john', 'john@example.com', locale='en')

        assert _flat(self._segment(campaign, {'locale__in': ['ru']})) == []

    def test_a_single_unknown_field_poisons_the_whole_condition(self, campaign):
        """Частично применённое условие — та же ошибка, только тише.

        Отбросив один ключ из двух, веер разослал бы письмо тем, кого менеджер
        из выборки как раз исключал.
        """
        _subscriber('ivan', 'ivan@example.com', locale='ru', timezone='Europe/Moscow')

        assert _flat(self._segment(campaign, {'locale': 'ru', 'email__endswith': '@example.com'})) == []
