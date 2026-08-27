"""Приём фиксированных событий: повтор не даёт второго письма.

Продюсер повторяет запрос при сетевой ошибке — это не исключительная ситуация, а
штатный режим. Поэтому проверяется не «событие обработалось», а «событие,
пришедшее дважды, обработалось один раз», и отдельно — что исходы «ничего не
сделали» не выглядят для продюсера как повод ретраить.
"""

import uuid

import pytest

from practix_notifications.campaigns.models import Campaign, EventBinding, OutboxMessage, ScheduledRun
from practix_notifications.content.models import MessageTemplate
from practix_notifications.enums import CampaignStatus, Category, DomainEvent, EventAudience
from practix_notifications.services import intake
from practix_notifications.subscribers.models import Subscriber

pytestmark = pytest.mark.django_db

USER_ID = '11111111-1111-1111-1111-111111111111'
FILM_ID = '22222222-2222-2222-2222-222222222222'


@pytest.fixture
def template() -> MessageTemplate:
    return MessageTemplate.objects.create(
        code='welcome', name='Приветствие', subject_template='Привет, {{ login }}', body_template='<p>Тело</p>'
    )


@pytest.fixture
def campaign(template) -> Campaign:
    return Campaign.objects.create(name='Приветственная', template=template, category=Category.SYSTEM.value)


@pytest.fixture
def welcome_binding(campaign) -> EventBinding:
    return EventBinding.objects.create(
        event_type=DomainEvent.USER_REGISTERED.value,
        campaign=campaign,
        audience=EventAudience.SUBJECT.value,
        upsert_subscriber=True,
    )


@pytest.fixture
def film_binding(campaign) -> EventBinding:
    return EventBinding.objects.create(
        event_type=DomainEvent.FILM_PUBLISHED.value,
        campaign=campaign,
        audience=EventAudience.CAMPAIGN.value,
    )


def _registration(**overrides) -> dict:
    payload = {
        'type': DomainEvent.USER_REGISTERED.value,
        'event_id': str(uuid.uuid4()),
        'occurred_at': '2026-08-05T10:00:00+00:00',
        'data': {'user_id': USER_ID, 'login': 'ivan', 'email': 'ivan@example.com'},
    }
    payload.update(overrides)
    return payload


def test_registration_creates_one_run_and_one_outbox_row(welcome_binding):
    result = intake.handle_domain_event(_registration())

    assert result.status == 202
    assert result.body['status'] == 'queued'
    assert ScheduledRun.objects.count() == 1
    assert OutboxMessage.objects.count() == 1


def test_repeated_registration_is_a_duplicate_even_with_a_new_event_id(welcome_binding):
    """Продюсер, перегенерировавший event_id на повторе, — обычное дело."""
    first = intake.handle_domain_event(_registration())
    second = intake.handle_domain_event(_registration())

    assert first.status == 202
    assert second.status == 200
    assert second.body == {'status': 'duplicate', 'run_id': first.body['run_id']}
    assert ScheduledRun.objects.count() == 1
    assert OutboxMessage.objects.count() == 1


def test_registration_creates_the_subscriber(welcome_binding):
    intake.handle_domain_event(_registration())

    subscriber = Subscriber.objects.get(pk=USER_ID)
    assert subscriber.email == 'ivan@example.com'
    assert subscriber.login == 'ivan'
    assert subscriber.is_active


def test_repeat_does_not_overwrite_a_locally_changed_timezone(welcome_binding):
    """Таймзону человек мог поменять у себя — событие о регистрации её не знает."""
    intake.handle_domain_event(_registration())
    Subscriber.objects.filter(pk=USER_ID).update(timezone='Asia/Vladivostok')

    intake.handle_domain_event(_registration(data={'user_id': USER_ID, 'login': 'ivan', 'email': 'ivan@example.com'}))

    assert Subscriber.objects.get(pk=USER_ID).timezone == 'Asia/Vladivostok'


def test_targeted_run_does_not_move_the_campaign_status(welcome_binding, campaign):
    """Письмо одному человеку — это не «рассылка идёт»."""
    intake.handle_domain_event(_registration())

    campaign.refresh_from_db()
    assert campaign.status == CampaignStatus.DRAFT.value
    assert ScheduledRun.objects.get().is_targeted


def test_film_event_is_a_mass_mailing(film_binding, campaign):
    result = intake.handle_domain_event(
        {
            'type': DomainEvent.FILM_PUBLISHED.value,
            'data': {'film_id': FILM_ID, 'title': 'Дюна', 'year': 2021},
        }
    )

    assert result.status == 202
    run = ScheduledRun.objects.get()
    assert not run.is_targeted
    assert run.context_overrides == {'film_title': 'Дюна', 'year': 2021}
    campaign.refresh_from_db()
    assert campaign.status == CampaignStatus.QUEUED.value


def test_same_film_is_announced_once(film_binding):
    data = {'film_id': FILM_ID, 'title': 'Дюна', 'year': 2021}
    intake.handle_domain_event({'type': DomainEvent.FILM_PUBLISHED.value, 'data': data})
    second = intake.handle_domain_event({'type': DomainEvent.FILM_PUBLISHED.value, 'data': data})

    assert second.body['status'] == 'duplicate'
    assert ScheduledRun.objects.count() == 1


def test_unknown_event_type_is_rejected_without_a_run():
    result = intake.handle_domain_event({'type': 'user.deleted', 'data': {}})

    assert result.status == 400
    assert ScheduledRun.objects.count() == 0


def test_missing_required_field_is_rejected(welcome_binding):
    result = intake.handle_domain_event(_registration(data={'user_id': USER_ID}))

    assert result.status == 400
    assert 'email' in result.body['detail']
    assert ScheduledRun.objects.count() == 0


def test_bad_occurred_at_is_rejected(welcome_binding):
    result = intake.handle_domain_event(_registration(occurred_at='вчера'))

    assert result.status == 400
    assert ScheduledRun.objects.count() == 0


def test_no_binding_is_ignored_not_retried():
    """200, а не 5xx: менеджер просто не сказал, чем отвечать на событие."""
    result = intake.handle_domain_event(_registration())

    assert result.status == 200
    assert result.body == {'status': 'ignored', 'reason': 'no_binding'}
    assert ScheduledRun.objects.count() == 0


def test_disabled_binding_is_ignored(welcome_binding):
    EventBinding.objects.filter(pk=welcome_binding.pk).update(is_enabled=False)

    result = intake.handle_domain_event(_registration())

    assert result.status == 200
    assert result.body['reason'] == 'binding_disabled'
    assert ScheduledRun.objects.count() == 0


def test_unknown_subscriber_is_ignored_when_the_binding_may_not_create_one(campaign):
    """Событие с чужими контактами не должно заводить людей само по себе."""
    EventBinding.objects.create(
        event_type=DomainEvent.USER_REGISTERED.value,
        campaign=campaign,
        audience=EventAudience.SUBJECT.value,
        upsert_subscriber=False,
    )

    result = intake.handle_domain_event(_registration())

    assert result.body == {'status': 'ignored', 'reason': 'unknown_subscriber'}


def test_recycled_email_is_ignored_not_five_hundred(welcome_binding):
    """Уникальность email: повтор запроса такое не вылечит, значит не 5xx."""
    Subscriber.objects.create(id=uuid.uuid4(), login='old', email='ivan@example.com')

    result = intake.handle_domain_event(_registration())

    assert result.status == 200
    assert result.body['reason'] == 'email_conflict'
    assert ScheduledRun.objects.count() == 0


def test_direct_messages_are_not_accepted_by_the_event_endpoint():
    """`type` там означает канал, здесь — имя события. Смешивать нельзя."""
    result = intake.handle_domain_event({'type': DomainEvent.NOTIFICATION_DIRECT.value, 'data': {'user_id': USER_ID}})

    assert result.status == 400
    assert 'messages' in result.body['detail']


def test_disabled_template_is_a_conflict_not_a_duplicate(welcome_binding, template):
    MessageTemplate.objects.filter(pk=template.pk).update(is_active=False)

    result = intake.handle_domain_event(_registration())

    assert result.status == 409
    assert ScheduledRun.objects.count() == 0


class TestCallerErrorsAreNotFiveHundreds:
    """Ошибка вызывающего обязана быть 4xx, а не падением обработчика.

    Идентификатор из тела запроса нельзя отдавать в ``filter(pk=...)`` как есть:
    у ``UUIDField`` это не пустая выборка, а исключение на уровне поля — то есть
    500 в ответ на опечатку. Для подписчика прикрытие было с самого начала, для
    рассылки его не было.
    """

    def test_malformed_campaign_id_is_a_404(self):
        result = intake.handle_campaign_launch({'campaign_id': 'не-uuid-вовсе'})

        assert result.status == 404
        assert result.body['detail'] == 'campaign not found'

    def test_missing_campaign_id_is_a_404(self):
        assert intake.handle_campaign_launch({}).status == 404

    def test_existing_campaign_still_launches(self, campaign):
        result = intake.handle_campaign_launch({'campaign_id': str(campaign.id)})

        assert result.status == 202
        assert ScheduledRun.objects.count() == 1

    def test_empty_event_type_is_rejected_as_an_event(self):
        """`{"type": ""}` — это событие с пустым именем, а не «нет поля type».

        Ветвление по истинности отправляло его в ветку запуска рассылки, и
        вызывающий получал «нужен campaign_id» про поле, которого он не слал.
        """
        result = intake.handle_domain_event({'type': '', 'data': {}})

        assert result.status == 400
        assert 'unknown event type' in result.body['detail']
