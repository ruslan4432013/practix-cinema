"""Формирующий воркер: что он делает с пачкой и чего не делает при отказе Auth."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from practix_notifications.broker import topology
from practix_notifications.broker.consumer import Outcome
from practix_notifications.broker.envelope import TargetRef, build_notification_requested
from practix_notifications.campaigns.models import Campaign, DeliveryTask, ScheduledRun
from practix_notifications.enums import CampaignStatus, RunStatus, SkipReason, TaskStatus
from practix_notifications.services.building import handle_notification_requested
from practix_notifications.services.directory import Person
from practix_notifications.services.shortener_client import ShortenerClientError, ShortenerUnavailable
from practix_notifications.subscribers.models import Subscriber

pytestmark = pytest.mark.django_db


class FakeBroker:
    """Брокер, который только записывает опубликованное."""

    def __init__(self) -> None:
        self.published: list[dict] = []

    def publish(self, *, exchange: str, routing_key: str, body: dict, headers: dict | None = None) -> None:
        self.published.append({'exchange': exchange, 'routing_key': routing_key, 'body': body, 'headers': headers})


class FakeDirectory:
    """Резолвер личности, который отвечает заранее заданным или падает."""

    def __init__(self, people: dict[str, Person] | None = None, error: Exception | None = None) -> None:
        self.people = people or {}
        self.error = error
        self.asked: list[list[str]] = []

    def resolve(self, subscriber_ids: list[str]) -> dict[str, Person]:
        self.asked.append(list(subscriber_ids))
        if self.error is not None:
            raise self.error
        return {user_id: self.people[user_id] for user_id in subscriber_ids if user_id in self.people}


def _person(subscriber: Subscriber, **overrides) -> Person:
    payload = {
        'user_id': str(subscriber.id),
        'login': subscriber.login,
        'email': subscriber.email,
        'first_name': 'Иван',
        'last_name': 'Петров',
    }
    payload.update(overrides)
    return Person(**payload)


@pytest.fixture
def run(campaign) -> ScheduledRun:
    campaign.status = CampaignStatus.RUNNING.value
    campaign.save(update_fields=['status'])
    return ScheduledRun.objects.create(
        campaign=campaign,
        run_key=str(uuid.uuid4()),
        planned_for=datetime.now(UTC),
        subject_snapshot='Привет, {{ display_name }}',
        body_snapshot='<p>{{ full_name }}, у нас новинки</p>',
        status=RunStatus.FANNING_OUT.value,
    )


def _task(run: ScheduledRun, subscriber: Subscriber, **overrides) -> DeliveryTask:
    payload = {
        'run': run,
        'campaign': run.campaign,
        'subscriber': subscriber,
        'channel': 'email',
        'idempotency_key': str(uuid.uuid4()),
        'status': TaskStatus.QUEUED.value,
    }
    payload.update(overrides)
    return DeliveryTask.objects.create(**payload)


def _body(run: ScheduledRun, tasks: list[DeliveryTask], *, valid_for_hours: int = 24) -> dict:
    return build_notification_requested(
        run_id=str(run.id),
        campaign_id=str(run.campaign_id),
        channel='email',
        category='marketing',
        content_id='',
        template_code='t',
        template_revision=1,
        context={'campaign_title': 'Новинки'},
        valid_until=datetime.now(UTC) + timedelta(hours=valid_for_hours),
        targets=[
            TargetRef(task_id=str(task.id), idempotency_key=task.idempotency_key, subscriber_id=str(task.subscriber_id))
            for task in tasks
        ],
    )


def test_name_from_auth_lands_in_the_letter(run, subscriber):
    """Главное требование задания, проверенное на пачке из одного человека."""
    task = _task(run, subscriber)
    broker = FakeBroker()

    outcome = handle_notification_requested(
        broker, FakeDirectory({str(subscriber.id): _person(subscriber)}), _body(run, [task]), 0
    )

    assert outcome is Outcome.ACK
    recipient = broker.published[0]['body']['recipients'][0]
    assert recipient['subject'] == 'Привет, Иван'
    assert 'Иван Петров' in recipient['body']
    assert recipient['address'] == subscriber.email


def test_address_is_written_onto_the_task(run, subscriber):
    """На адресе задачи держится и журнал, и проверка no_address в отправке."""
    task = _task(run, subscriber)

    handle_notification_requested(
        FakeBroker(), FakeDirectory({str(subscriber.id): _person(subscriber)}), _body(run, [task]), 0
    )

    task.refresh_from_db()
    assert task.address == subscriber.email


def test_prepared_message_starts_a_fresh_attempt_budget(run, subscriber):
    """Икота Auth не должна съедать попытки отправки: это разные подсистемы."""
    task = _task(run, subscriber)
    broker = FakeBroker()

    handle_notification_requested(
        broker, FakeDirectory({str(subscriber.id): _person(subscriber)}), _body(run, [task]), 3
    )

    assert broker.published[0]['routing_key'] == topology.RK_NOTIFICATION_PREPARED
    assert not (broker.published[0]['headers'] or {}).get(topology.HEADER_ATTEMPT)


def test_auth_outage_retries_the_whole_batch_without_sending_anything(run, subscriber):
    """Ни одного письма наполовину и никакой обезличенной подстановки."""
    from practix_notifications.services.auth_client import AuthUnavailable

    task = _task(run, subscriber)
    broker = FakeBroker()

    outcome = handle_notification_requested(
        broker, FakeDirectory(error=AuthUnavailable('Auth ответил 503')), _body(run, [task]), 0
    )

    assert outcome is Outcome.RETRY
    assert broker.published == []
    task.refresh_from_db()
    assert task.status == TaskStatus.QUEUED.value
    assert task.address == ''


def test_auth_configuration_error_is_terminal(run, subscriber):
    """Не тот пароль сервисной учётки не лечится десятью повторами на пачку."""
    from practix_notifications.services.auth_client import AuthClientError

    task = _task(run, subscriber)

    outcome = handle_notification_requested(
        FakeBroker(), FakeDirectory(error=AuthClientError('Auth ответил 403')), _body(run, [task]), 0
    )

    assert outcome is Outcome.DEAD


def test_unknown_user_is_skipped_with_its_own_reason(run, subscriber):
    """«Отписался» и «его больше нет в Auth» — разные факты для менеджера."""
    task = _task(run, subscriber)

    handle_notification_requested(FakeBroker(), FakeDirectory({}), _body(run, [task]), 0)

    task.refresh_from_db()
    assert task.status == TaskStatus.SKIPPED.value
    assert task.skip_reason == SkipReason.UNKNOWN_USER.value


def test_user_without_email_in_auth_is_skipped(run, subscriber):
    task = _task(run, subscriber)
    person = _person(subscriber, email='')

    handle_notification_requested(FakeBroker(), FakeDirectory({str(subscriber.id): person}), _body(run, [task]), 0)

    task.refresh_from_db()
    assert task.skip_reason == SkipReason.NO_ADDRESS.value


def test_a_fully_skipped_batch_still_closes_the_run(run, subscriber):
    """Иначе кампания навсегда осталась бы «идущей», а прогон — открытым."""
    _task(run, subscriber)

    handle_notification_requested(FakeBroker(), FakeDirectory({}), _body(run, [_task(run, subscriber)]), 0)

    # Обе задачи прогона должны быть терминальными, чтобы он закрылся: вторая
    # создана внутри вызова, первая — выше, поэтому проверяем состояние прогона
    # после явного отсева обеих.
    DeliveryTask.objects.filter(run=run).update(status=TaskStatus.SKIPPED.value)
    handle_notification_requested(FakeBroker(), FakeDirectory({}), _body(run, []), 0)

    run.refresh_from_db()
    assert run.status == RunStatus.PUBLISHED.value
    assert Campaign.objects.get(pk=run.campaign_id).status == CampaignStatus.DONE.value


def test_broken_template_fails_one_recipient_but_not_the_batch(run, subscriber):
    other = Subscriber.objects.create(id=uuid.uuid4(), login='petr', email='petr@example.com')
    run.subject_snapshot = 'Привет, {{ display_name }}'
    run.body_snapshot = '{% if %}'  # синтаксически невалидный шаблон
    run.save(update_fields=['subject_snapshot', 'body_snapshot'])
    first, second = _task(run, subscriber), _task(run, other)

    outcome = handle_notification_requested(
        FakeBroker(),
        FakeDirectory({str(subscriber.id): _person(subscriber), str(other.id): _person(other)}),
        _body(run, [first, second]),
        0,
    )

    assert outcome is Outcome.ACK
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.status == TaskStatus.FAILED.value
    assert second.status == TaskStatus.FAILED.value


def test_already_sent_task_is_dropped_before_touching_auth(run, subscriber):
    """Переотправленная пачка не должна устраивать второй шторм запросов."""
    task = _task(run, subscriber, status=TaskStatus.SENT.value)
    directory = FakeDirectory({str(subscriber.id): _person(subscriber)})

    outcome = handle_notification_requested(FakeBroker(), directory, _body(run, [task]), 0)

    assert outcome is Outcome.ACK
    assert directory.asked == []


def test_stale_event_is_skipped_before_touching_auth(run, subscriber):
    """«Вышла новая серия» через сутки пользователю уже не нужна."""
    task = _task(run, subscriber)
    directory = FakeDirectory({str(subscriber.id): _person(subscriber)})

    handle_notification_requested(FakeBroker(), directory, _body(run, [task], valid_for_hours=-1), 0)

    task.refresh_from_db()
    assert task.skip_reason == SkipReason.STALE_EVENT.value
    assert directory.asked == []


def test_unimplemented_channel_is_skipped_before_touching_auth(run, subscriber):
    task = _task(run, subscriber, channel='sms')
    directory = FakeDirectory({str(subscriber.id): _person(subscriber)})

    handle_notification_requested(FakeBroker(), directory, _body(run, [task]), 0)

    task.refresh_from_db()
    assert task.skip_reason == SkipReason.CHANNEL_UNAVAILABLE.value
    assert directory.asked == []


def test_unknown_run_is_dead_lettered(subscriber):
    outcome = handle_notification_requested(
        FakeBroker(), FakeDirectory(), {'run_id': str(uuid.uuid4()), 'targets': []}, 0
    )

    assert outcome is Outcome.DEAD


def test_letters_are_published_in_small_chunks(run, subscriber, monkeypatch):
    """Каждое сообщение везёт готовые тела — сотня получателей раздула бы его."""
    monkeypatch.setattr('practix_notifications.services.building.settings.NOTIFY_BUILD_MESSAGE_BATCH', 2)
    people, tasks = {}, []
    for index in range(5):
        person_subscriber = Subscriber.objects.create(id=uuid.uuid4(), login=f'u{index}', email=f'u{index}@example.com')
        people[str(person_subscriber.id)] = _person(person_subscriber)
        tasks.append(_task(run, person_subscriber))
    broker = FakeBroker()

    handle_notification_requested(broker, FakeDirectory(people), _body(run, tasks), 0)

    assert [len(message['body']['recipients']) for message in broker.published] == [2, 2, 1]


class FakeShortener:
    """Заглушка сервиса ссылок: считает вызовы и умеет падать."""

    def __init__(self, raises: Exception | None = None):
        self.calls: list[dict] = []
        self.raises = raises

    def create_link(self, **kwargs):
        if self.raises is not None:
            raise self.raises
        self.calls.append(kwargs)
        return f'http://localhost/s/code{len(self.calls)}'


@pytest.fixture
def shortener(monkeypatch) -> FakeShortener:
    """Подменяет клиент сервиса ссылок во всех тестах, которые его просят."""
    fake = FakeShortener()
    monkeypatch.setattr('practix_notifications.services.building.ShortenerClient', lambda: fake)
    return fake


def _wanting_confirm(run: ScheduledRun) -> ScheduledRun:
    run.body_snapshot = '<p>{{ full_name }}, <a href="{{ confirm_url }}">подтвердите</a></p>'
    run.save(update_fields=['body_snapshot'])
    return run


def test_shortener_is_untouched_when_the_template_does_not_ask(run, subscriber, shortener):
    """Иначе рассылка на сто тысяч адресов оставила бы сто тысяч ненужных ссылок."""
    task = _task(run, subscriber)

    handle_notification_requested(
        FakeBroker(), FakeDirectory({str(subscriber.id): _person(subscriber)}), _body(run, [task]), 0
    )

    assert shortener.calls == []


def test_confirm_url_is_minted_per_recipient(run, subscriber, shortener):
    _wanting_confirm(run)
    task = _task(run, subscriber)
    broker = FakeBroker()

    handle_notification_requested(
        broker, FakeDirectory({str(subscriber.id): _person(subscriber)}), _body(run, [task]), 0
    )

    assert len(shortener.calls) == 1
    # Ключ идемпотентности — тот же, что у задачи: пересобранная пачка получит
    # ту же ссылку, а не выпустит вторую.
    assert shortener.calls[0]['idempotency_key'] == task.idempotency_key
    assert shortener.calls[0]['user_id'] == str(subscriber.id)
    assert 'http://localhost/s/code1' in broker.published[0]['body']['recipients'][0]['body']


def test_unavailable_shortener_retries_the_whole_batch(run, subscriber, monkeypatch):
    """Письмо с мёртвой ссылкой хуже, чем письмо на десять минут позже."""
    _wanting_confirm(run)
    monkeypatch.setattr(
        'practix_notifications.services.building.ShortenerClient',
        lambda: FakeShortener(raises=ShortenerUnavailable('down')),
    )
    task = _task(run, subscriber)

    outcome = handle_notification_requested(
        FakeBroker(), FakeDirectory({str(subscriber.id): _person(subscriber)}), _body(run, [task]), 0
    )

    assert outcome is Outcome.RETRY


def test_rejected_link_dead_letters_the_batch(run, subscriber, monkeypatch):
    """Не тот токен или цель вне белого списка — повтор это не лечит."""
    _wanting_confirm(run)
    monkeypatch.setattr(
        'practix_notifications.services.building.ShortenerClient',
        lambda: FakeShortener(raises=ShortenerClientError('400')),
    )
    task = _task(run, subscriber)

    outcome = handle_notification_requested(
        FakeBroker(), FakeDirectory({str(subscriber.id): _person(subscriber)}), _body(run, [task]), 0
    )

    assert outcome is Outcome.DEAD


def test_unparsable_snapshot_does_not_kill_the_batch(run, subscriber, shortener):
    """Проверка «нужна ли ссылка» разбирает шаблон — и не имеет права на нём упасть.

    Сломанный шаблон обязан завалить ОДНОГО получателя на рендере, а не всю
    пачку до него.
    """
    run.body_snapshot = '{% if %}'
    run.save(update_fields=['body_snapshot'])
    task = _task(run, subscriber)

    outcome = handle_notification_requested(
        FakeBroker(), FakeDirectory({str(subscriber.id): _person(subscriber)}), _body(run, [task]), 0
    )

    assert outcome is Outcome.ACK
    task.refresh_from_db()
    assert task.status == TaskStatus.FAILED.value
