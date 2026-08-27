"""Формы админки: то, что менеджер увидит вместо падения на рассылке."""

from datetime import UTC, datetime

import pytest

from practix_notifications.campaigns.forms import CampaignForm, CampaignScheduleForm
from practix_notifications.campaigns.models import Campaign
from practix_notifications.content.models import MessageTemplate
from practix_notifications.enums import Category, Channel, ScheduleKind

pytestmark = pytest.mark.django_db


@pytest.fixture
def template() -> MessageTemplate:
    return MessageTemplate.objects.create(
        code='weekly',
        name='Подборка',
        channel=Channel.EMAIL.value,
        subject_template='Привет, {{ login }}',
        body_template='<p>{{ login }}</p>',
        allowed_variables=['login'],
        sample_context={'login': 'ivan'},
    )


def campaign_data(template: MessageTemplate, **overrides) -> dict:
    data = {
        'name': 'Рассылка',
        'description': '',
        'channel': Channel.EMAIL.value,
        'template': template.pk,
        'category': Category.MARKETING.value,
        'content_id': '',
        'context': '{}',
        'status': 'draft',
        'respect_quiet_hours': True,
        'recipients_total': 0,
        'sent_count': 0,
        'failed_count': 0,
        'skipped_count': 0,
    }
    data.update(overrides)
    return data


def test_campaign_accepts_matching_channel(template):
    assert CampaignForm(data=campaign_data(template)).is_valid()


def test_campaign_rejects_channel_mismatch(template):
    """Иначе HTML-письмо уехало бы в SMS-отправителя — и выяснилось бы на рассылке."""
    form = CampaignForm(data=campaign_data(template, channel=Channel.SMS.value))
    assert not form.is_valid()
    assert 'template' in form.errors


def test_campaign_rejects_disabled_template(template):
    template.is_active = False
    template.save(update_fields=['is_active'])
    form = CampaignForm(data=campaign_data(template))
    assert not form.is_valid()
    assert 'template' in form.errors


def schedule_data(campaign: Campaign, **overrides) -> dict:
    data = {
        'campaign': campaign.pk,
        'kind': ScheduleKind.IMMEDIATE.value,
        'timezone': 'Europe/Moscow',
        'is_enabled': True,
        'runs_count': 0,
    }
    data.update(overrides)
    return data


@pytest.fixture
def campaign(template) -> Campaign:
    return Campaign.objects.create(name='Рассылка', template=template, channel=Channel.EMAIL.value)


def test_defer_hours_becomes_absolute_moment(campaign):
    """«Через N часов» — виджет формы; в базу уходит абсолютный момент."""
    before = datetime.now(UTC)
    form = CampaignScheduleForm(data=schedule_data(campaign, defer_hours=3))
    assert form.is_valid(), form.errors

    run_at = form.cleaned_data['run_at']
    assert form.cleaned_data['kind'] == ScheduleKind.DEFERRED.value
    assert form.cleaned_data['next_run_at'] == run_at
    assert 2.9 < (run_at - before).total_seconds() / 3600 < 3.1


def test_deferred_without_moment_rejected(campaign):
    form = CampaignScheduleForm(data=schedule_data(campaign, kind=ScheduleKind.DEFERRED.value))
    assert not form.is_valid()
    assert 'run_at' in form.errors


def test_recurring_computes_first_run(campaign):
    form = CampaignScheduleForm(
        data=schedule_data(campaign, kind=ScheduleKind.RECURRING.value, cron_expression='0 12 * * 5')
    )
    assert form.is_valid(), form.errors
    first = form.cleaned_data['next_run_at']
    assert first > datetime.now(UTC)
    # Полдень по Москве — 09:00 UTC.
    assert first.weekday() == 4


def test_recurring_rejects_broken_cron(campaign):
    form = CampaignScheduleForm(
        data=schedule_data(campaign, kind=ScheduleKind.RECURRING.value, cron_expression='каждую пятницу')
    )
    assert not form.is_valid()
    assert 'cron_expression' in form.errors


def test_recurring_requires_cron(campaign):
    form = CampaignScheduleForm(data=schedule_data(campaign, kind=ScheduleKind.RECURRING.value))
    assert not form.is_valid()
    assert 'cron_expression' in form.errors


class TestTemplateForm:
    """Форма шаблона — та же валидация, что и у приёма событий."""

    def test_email_template_requires_subject(self):
        from practix_notifications.content.forms import MessageTemplateForm

        form = MessageTemplateForm(
            data={
                'code': 'no-subject',
                'name': 'Без темы',
                'channel': Channel.EMAIL.value,
                'body_template': 'привет',
                'body_format': 'html',
                'allowed_variables': '[]',
                'sample_context': '{}',
                'revision': 1,
                'is_active': True,
                'description': '',
                'subject_template': '',
            }
        )
        assert not form.is_valid()
        assert 'subject_template' in form.errors

    def test_forbidden_variable_surfaces_on_the_field(self):
        from practix_notifications.content.forms import MessageTemplateForm

        form = MessageTemplateForm(
            data={
                'code': 'leaky',
                'name': 'Утечка',
                'channel': Channel.EMAIL.value,
                'subject_template': 'Привет',
                'body_template': '{{ secret }}',
                'body_format': 'html',
                'allowed_variables': '["login"]',
                'sample_context': '{"login": "ivan"}',
                'revision': 1,
                'is_active': True,
                'description': '',
            }
        )
        assert not form.is_valid()
        assert any('secret' in error for error in form.errors['body_template'])


class TestSegmentForm:
    """Условие сегмента проверяется при сохранении, а не при рассылке.

    Веер применяет только поля из белого списка и на любом чужом ключе считает
    аудиторию пустой. Узнать об этом менеджер должен здесь — иначе он узнаёт из
    отчёта «отправлено 0» через полчаса после запуска.
    """

    @staticmethod
    def _form(**overrides):
        from practix_notifications.enums import SegmentKind
        from practix_notifications.subscribers.admin import SegmentAdminForm

        data = {'code': 'ru', 'name': 'Русскоязычные', 'kind': SegmentKind.FILTER.value, 'filter': '{"locale": "ru"}'}
        data.update(overrides)
        return SegmentAdminForm(data=data)

    def test_whitelisted_field_is_accepted(self):
        assert self._form().is_valid()

    def test_unknown_field_is_rejected_with_the_allowed_list(self):
        form = self._form(filter='{"email__endswith": "@example.com"}')

        assert not form.is_valid()
        assert any('email__endswith' in error for error in form.errors['filter'])
        assert any('locale' in error for error in form.errors['filter'])

    def test_empty_condition_is_rejected(self):
        """Пустое условие — это `filter()`, то есть вся база под видом сегмента."""
        form = self._form(filter='{}')

        assert not form.is_valid()
        assert 'filter' in form.errors

    def test_static_segment_ignores_the_condition(self):
        from practix_notifications.enums import SegmentKind

        assert self._form(kind=SegmentKind.STATIC.value, filter='{}').is_valid()
