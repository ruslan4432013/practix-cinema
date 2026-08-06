"""Формы рассылки и расписания.

Главное здесь — поле «Отправить через N часов». Оно НЕ ХРАНИТСЯ: в базу уходит
абсолютный момент ``run_at``. Сохранив «через 3 часа», сервис не смог бы ответить,
от какого момента отсчитывать после перезапуска планировщика, — а отложенная
рассылка обязана пережить перезапуск.
"""

from datetime import UTC, datetime

from django import forms

from practix_notifications.campaigns.models import Campaign, CampaignSchedule
from practix_notifications.enums import ScheduleKind
from practix_notifications.services.scheduling import ScheduleError, deferred_run_at, next_run_at, validate_cron


class CampaignForm(forms.ModelForm):
    class Meta:
        model = Campaign
        fields = '__all__'

    def clean(self) -> dict:
        cleaned = super().clean()
        template = cleaned.get('template')
        channel = cleaned.get('channel')
        if template is not None and channel and template.channel != channel:
            # Иначе HTML-письмо уехало бы в SMS-отправителя, и выяснилось бы это
            # на рассылке, а не на форме.
            self.add_error('template', f'Шаблон рассчитан на канал «{template.channel}», а рассылка — на «{channel}»')
        if template is not None and not template.is_active:
            self.add_error('template', 'Шаблон выключен')
        return cleaned


class CampaignScheduleForm(forms.ModelForm):
    defer_hours = forms.FloatField(
        required=False,
        min_value=0.01,
        label='Отправить через (часов)',
        help_text='Заполните вместо «Отправить в» — момент будет посчитан от сохранения',
    )

    class Meta:
        model = CampaignSchedule
        fields = '__all__'

    def clean(self) -> dict:
        cleaned = super().clean()
        kind = cleaned.get('kind')
        hours = cleaned.get('defer_hours')

        if hours:
            cleaned['kind'] = kind = ScheduleKind.DEFERRED.value
            cleaned['run_at'] = deferred_run_at(datetime.now(UTC), hours)

        if kind == ScheduleKind.DEFERRED.value and not cleaned.get('run_at'):
            self.add_error('run_at', 'Укажите момент отправки или отсрочку в часах')

        if kind == ScheduleKind.RECURRING.value:
            expression = (cleaned.get('cron_expression') or '').strip()
            if not expression:
                self.add_error('cron_expression', 'Для повторяющейся рассылки нужно расписание')
            else:
                try:
                    validate_cron(expression)
                    # Первое срабатывание считаем сразу: расписание, добавленное
                    # без next_run_at, никогда бы не проснулось.
                    cleaned['next_run_at'] = next_run_at(
                        expression,
                        after=cleaned.get('starts_at') or datetime.now(UTC),
                        timezone=cleaned.get('timezone') or 'UTC',
                    )
                except ScheduleError as exc:
                    self.add_error('cron_expression', str(exc))

        if kind == ScheduleKind.DEFERRED.value and cleaned.get('run_at'):
            cleaned['next_run_at'] = cleaned['run_at']

        if kind == ScheduleKind.IMMEDIATE.value:
            # Немедленная рассылка запускается кнопкой, а не расписанием.
            cleaned['next_run_at'] = None

        return cleaned
