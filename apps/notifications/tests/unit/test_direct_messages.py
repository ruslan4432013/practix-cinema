"""Сообщения в свободном формате: шаблон или сырой текст, но всегда проверенный.

Сырой текст от вызывающего — это шаблон, и относиться к нему иначе, чем к
шаблону менеджера, нельзя: те же три проверки теории (синтаксис, разрешённые
возможности, рендерится ли) и тот же очищенный набор глобалов. Разница только в
том, что вызывающий узнаёт о поломке синхронно, кодом ответа.
"""

import uuid

import pytest

from practix_notifications.campaigns.models import Campaign, EventBinding, ScheduledRun
from practix_notifications.content.models import MessageTemplate
from practix_notifications.enums import Channel, DomainEvent, EventAudience
from practix_notifications.services import intake
from practix_notifications.subscribers.models import Subscriber

pytestmark = pytest.mark.django_db

USER_ID = '11111111-1111-1111-1111-111111111111'


@pytest.fixture
def subscriber() -> Subscriber:
    return Subscriber.objects.create(id=USER_ID, login='ivan', email='ivan@example.com')


@pytest.fixture
def direct_binding() -> EventBinding:
    template = MessageTemplate.objects.create(
        code='direct-default', name='Запасной', subject_template='Уведомление', body_template='<p>Тело</p>'
    )
    campaign = Campaign.objects.create(name='Прямые сообщения', template=template)
    return EventBinding.objects.create(
        event_type=DomainEvent.NOTIFICATION_DIRECT.value,
        campaign=campaign,
        audience=EventAudience.SUBJECT.value,
    )


def _message(**overrides) -> dict:
    payload = {'event_id': str(uuid.uuid4()), 'user_id': USER_ID, 'text': 'Привет, {{ login }}!'}
    payload.update(overrides)
    return payload


def test_template_id_snapshots_the_template(direct_binding, subscriber):
    MessageTemplate.objects.create(
        code='promo', name='Акция', subject_template='Скидка', body_template='<p>-20%</p>', revision=7
    )

    result = intake.handle_direct_message(_message(text=None, template_id='promo'))

    assert result.status == 202
    run = ScheduledRun.objects.get()
    assert run.subject_snapshot == 'Скидка'
    assert run.body_snapshot == '<p>-20%</p>'
    assert run.template_revision == 7


def test_unknown_template_is_404(direct_binding, subscriber):
    result = intake.handle_direct_message(_message(text=None, template_id='нет-такого'))

    assert result.status == 404
    assert ScheduledRun.objects.count() == 0


def test_raw_text_is_stored_verbatim(direct_binding, subscriber):
    result = intake.handle_direct_message(_message(subject='Тема', text='Привет, {{ login }}!'))

    assert result.status == 202
    run = ScheduledRun.objects.get()
    assert run.subject_snapshot == 'Тема'
    assert run.body_snapshot == 'Привет, {{ login }}!'
    # Поле в контракте называется `text`, и autoescape положил бы в письмо
    # `&quot;` вместо кавычек.
    assert run.is_html is False


def test_html_format_is_opt_in(direct_binding, subscriber):
    result = intake.handle_direct_message(_message(text='<p>Привет</p>', format='html'))

    assert result.status == 202
    assert ScheduledRun.objects.get().is_html is True


def test_unknown_variable_is_rejected_at_intake(direct_binding, subscriber):
    result = intake.handle_direct_message(_message(text='Ваш баланс: {{ balance }}'))

    assert result.status == 400
    assert result.body['errors']
    assert ScheduledRun.objects.count() == 0


def test_caller_may_bring_its_own_variables(direct_binding, subscriber):
    """Свои переменные можно; дотянуться до чего-то ещё — нельзя."""
    result = intake.handle_direct_message(_message(text='Ваш баланс: {{ balance }}', context={'balance': 100}))

    assert result.status == 202
    assert ScheduledRun.objects.get().context_overrides == {'balance': 100}


def test_stripped_globals_are_rejected(direct_binding, subscriber):
    """Без range в шаблоне нечего итерировать — вечный цикл недостижим."""
    result = intake.handle_direct_message(_message(text='{% for i in range(10000000) %}x{% endfor %}'))

    assert result.status == 400
    assert ScheduledRun.objects.count() == 0


def test_broken_syntax_is_rejected(direct_binding, subscriber):
    result = intake.handle_direct_message(_message(text='{% if %}'))

    assert result.status == 400


def test_template_and_raw_text_are_mutually_exclusive(direct_binding, subscriber):
    result = intake.handle_direct_message(_message(template_id='direct-default', text='Привет'))

    assert result.status == 400
    assert 'mutually exclusive' in result.body['detail']


def test_neither_template_nor_text_is_rejected(direct_binding, subscriber):
    result = intake.handle_direct_message(_message(text=None))

    assert result.status == 400


def test_event_id_is_required(direct_binding, subscriber):
    """Природного ключа нет: два одинаковых напоминания — легитимный сценарий."""
    result = intake.handle_direct_message({'user_id': USER_ID, 'text': 'Привет'})

    assert result.status == 400
    assert 'deduplication key' in result.body['detail']


def test_same_event_id_gives_one_run(direct_binding, subscriber):
    payload = _message()
    intake.handle_direct_message(payload)
    second = intake.handle_direct_message(payload)

    assert second.body['status'] == 'duplicate'
    assert ScheduledRun.objects.count() == 1


def test_different_event_id_gives_two_runs(direct_binding, subscriber):
    intake.handle_direct_message(_message())
    intake.handle_direct_message(_message())

    assert ScheduledRun.objects.count() == 2


def test_sms_is_accepted_and_recorded_as_a_channel_override(direct_binding, subscriber):
    """Канал не реализован — но заявку принимаем: отказ 400 заставил бы
    вызывающего разбираться, какие каналы включены, а журнал и так это покажет."""
    result = intake.handle_direct_message(_message(type=Channel.SMS.value))

    assert result.status == 202
    assert ScheduledRun.objects.get().channel_override == Channel.SMS.value


def test_channel_is_an_accepted_alias_of_type(direct_binding, subscriber):
    result = intake.handle_direct_message(_message(channel=Channel.PUSH.value))

    assert result.status == 202
    assert ScheduledRun.objects.get().channel_override == Channel.PUSH.value


def test_unknown_channel_is_rejected(direct_binding, subscriber):
    result = intake.handle_direct_message(_message(type='telegram'))

    assert result.status == 400


def test_unknown_subscriber_is_ignored(direct_binding):
    result = intake.handle_direct_message(_message(user_id='33333333-3333-3333-3333-333333333333'))

    assert result.body == {'status': 'ignored', 'reason': 'unknown_subscriber'}


def test_malformed_user_id_is_ignored_not_five_hundred(direct_binding):
    """Невалидный UUID в фильтре дал бы DataError, то есть 500 на ошибку вызывающего."""
    result = intake.handle_direct_message(_message(user_id='не-uuid'))

    assert result.status == 200
    assert result.body['reason'] == 'unknown_subscriber'


def test_raw_text_can_be_switched_off(direct_binding, subscriber, settings_override):
    result = intake.handle_direct_message(_message(text='Привет'))

    assert result.status == 409
    assert 'template_id' in result.body['detail']


@pytest.fixture
def settings_override(monkeypatch):
    from practix_notifications.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, 'NOTIFY_FREEFORM_RAW_TEXT_ENABLED', False)
    return app_settings
