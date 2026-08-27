"""Админ-панель менеджера: создать рассылку и отправить её.

Три действия закрывают три сценария задания: «отправить сразу», «через N часов»
и «каждую пятницу». Первое — кнопка, два других — расписание, которое подхватит
планировщик.

Кнопка НЕ ОТПРАВЛЯЕТ письма и не собирает получателей. Она создаёт прогон и
кладёт событие в outbox — и возвращает управление менеджеру мгновенно, сколько бы
адресов ни было в сегменте. Всё остальное происходит за пределами HTTP-запроса.

Прогоны, задачи доставки и попытки зарегистрированы только на чтение: это
журнал, и правка его руками ломала бы идемпотентность, ради которой он и ведётся.
"""

from datetime import UTC, datetime

from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest

from practix_notifications.campaigns.forms import CampaignForm, CampaignScheduleForm
from practix_notifications.campaigns.models import (
    Campaign,
    CampaignSchedule,
    DeliveryAttempt,
    DeliveryTask,
    EventBinding,
    NotificationSubscription,
    OutboxMessage,
    ScheduledRun,
)
from practix_notifications.enums import CampaignStatus, ScheduleKind
from practix_notifications.services.launch import LaunchError, launch_now
from practix_notifications.services.scheduling import ScheduleError, next_run_at


class CampaignScheduleInline(admin.StackedInline):
    model = CampaignSchedule
    form = CampaignScheduleForm
    can_delete = False
    extra = 0
    readonly_fields = ('next_run_at', 'last_run_at', 'runs_count')
    fieldsets = (
        (None, {'fields': ('kind', 'is_enabled')}),
        ('Отложенная', {'fields': ('defer_hours', 'run_at')}),
        (
            'Повторяющаяся',
            {
                'fields': ('cron_expression', 'timezone', 'starts_at', 'ends_at', 'max_runs', 'catchup'),
                'description': (
                    '«0 12 * * 5» — каждую пятницу в полдень, «0 10 1 1 *» — каждый Новый год. '
                    '«Догонять пропущенные» выключено: после простоя сработает только последний '
                    'пропущенный запуск, а не все сразу.'
                ),
            },
        ),
        ('Состояние', {'fields': ('next_run_at', 'last_run_at', 'runs_count')}),
    )


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    form = CampaignForm
    inlines = [CampaignScheduleInline]
    list_display = (
        'name',
        'channel',
        'category',
        'status',
        'recipients_total',
        'sent_count',
        'failed_count',
        'skipped_count',
        'last_notification_sent_at',
    )
    list_filter = ('status', 'channel', 'category')
    search_fields = ('name', 'description', 'content_id')
    autocomplete_fields = ('template', 'segment')
    readonly_fields = (
        'status',
        'recipients_total',
        'sent_count',
        'failed_count',
        'skipped_count',
        'last_notification_sent_at',
        'created_at',
        'updated_at',
    )
    actions = ('action_launch_now', 'action_enable_schedule', 'action_cancel')

    def save_model(self, request: HttpRequest, obj: Campaign, form, change) -> None:
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    @admin.action(description='Отправить сейчас')
    def action_launch_now(self, request: HttpRequest, queryset: QuerySet[Campaign]) -> None:
        launched = skipped = 0
        for campaign in queryset.select_related('template'):
            if campaign.status == CampaignStatus.CANCELLED.value:
                skipped += 1
                continue
            try:
                run = launch_now(campaign)
            except LaunchError as exc:
                self.message_user(request, f'{campaign.name}: {exc}', level=messages.ERROR)
                continue
            if run is None:
                # Тот же ключ запуска в пределах секунды — двойной клик.
                skipped += 1
            else:
                launched += 1

        if launched:
            self.message_user(
                request,
                f'Поставлено в очередь: {launched}. Получателей соберёт планировщик — статус обновится сам.',
                level=messages.SUCCESS,
            )
        if skipped:
            self.message_user(request, f'Пропущено (уже запущены или отменены): {skipped}', level=messages.WARNING)

    @admin.action(description='Включить расписание')
    def action_enable_schedule(self, request: HttpRequest, queryset: QuerySet[Campaign]) -> None:
        enabled = 0
        for campaign in queryset.select_related('schedule'):
            schedule = getattr(campaign, 'schedule', None)
            if schedule is None or schedule.kind == ScheduleKind.IMMEDIATE.value:
                self.message_user(request, f'{campaign.name}: расписания нет', level=messages.WARNING)
                continue
            try:
                schedule.next_run_at = self._compute_next(schedule)
            except ScheduleError as exc:
                self.message_user(request, f'{campaign.name}: {exc}', level=messages.ERROR)
                continue
            schedule.is_enabled = True
            schedule.save(update_fields=['next_run_at', 'is_enabled'])
            Campaign.objects.filter(pk=campaign.pk).update(status=CampaignStatus.SCHEDULED.value)
            enabled += 1
        if enabled:
            self.message_user(request, f'Расписаний включено: {enabled}', level=messages.SUCCESS)

    @admin.action(description='Отменить')
    def action_cancel(self, request: HttpRequest, queryset: QuerySet[Campaign]) -> None:
        # Отменяется будущее, а не прошлое: уже опубликованные в брокер пачки
        # остановить нельзя, и обещать это в интерфейсе было бы враньём.
        CampaignSchedule.objects.filter(campaign__in=queryset).update(is_enabled=False, next_run_at=None)
        updated = queryset.update(status=CampaignStatus.CANCELLED.value)
        self.message_user(
            request,
            f'Отменено: {updated}. Пачки, уже ушедшие в очередь, будут доставлены.',
            level=messages.SUCCESS,
        )

    @staticmethod
    def _compute_next(schedule: CampaignSchedule):
        if schedule.kind == ScheduleKind.DEFERRED.value:
            return schedule.run_at
        return next_run_at(
            schedule.cron_expression,
            after=schedule.starts_at or datetime.now(UTC),
            timezone=schedule.timezone,
        )


class DeliveryAttemptInline(admin.TabularInline):
    model = DeliveryAttempt
    extra = 0
    can_delete = False
    readonly_fields = ('attempt_no', 'result', 'error', 'request_id', 'started_at', 'finished_at')

    def has_add_permission(self, request: HttpRequest, obj=None) -> bool:
        return False


@admin.register(EventBinding)
class EventBindingAdmin(admin.ModelAdmin):
    """Единственная редактируемая точка, где имя чужого события встречает рассылку.

    Редактируемая намеренно: «каким письмом встречать нового пользователя» —
    решение менеджера, и менять его редеплоем неправильно. Выключатель
    ``is_enabled`` гасит триггер, не удаляя привязку, — событие после этого
    принимается и игнорируется, а не отвергается ошибкой.
    """

    list_display = ('event_type', 'campaign', 'audience', 'upsert_subscriber', 'is_enabled', 'updated_at')
    list_filter = ('is_enabled', 'audience', 'event_type')
    search_fields = ('event_type', 'campaign__name', 'description')
    autocomplete_fields = ('campaign',)
    readonly_fields = ('created_at', 'updated_at')
    fieldsets = (
        (None, {'fields': ('event_type', 'campaign', 'is_enabled')}),
        (
            'Поведение',
            {
                'fields': ('audience', 'upsert_subscriber'),
                'description': (
                    '«Только тот, о ком событие» — письмо одному человеку, статус рассылки при этом '
                    'не меняется. «Аудитория рассылки» — обычный веер по сегменту. '
                    '«Заводить подписчика из события» нужно там, где человек может быть ещё не '
                    'синхронизирован из Auth, — то есть на регистрации.'
                ),
            },
        ),
        ('Комментарий', {'fields': ('description', 'created_at', 'updated_at')}),
    )


@admin.register(ScheduledRun)
class ScheduledRunAdmin(admin.ModelAdmin):
    list_display = (
        'run_key',
        'campaign',
        'event_type',
        'planned_for',
        'status',
        'tasks_created',
        'started_at',
        'finished_at',
    )
    list_filter = ('status', 'event_type')
    search_fields = ('run_key', 'campaign__name', 'event_id')
    readonly_fields = tuple(field.name for field in ScheduledRun._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj=None) -> bool:
        return False


@admin.register(DeliveryTask)
class DeliveryTaskAdmin(admin.ModelAdmin):
    list_display = ('address', 'campaign', 'channel', 'status', 'skip_reason', 'attempts', 'sent_at')
    list_filter = ('status', 'channel', 'skip_reason')
    search_fields = ('address', 'idempotency_key', 'campaign__name')
    readonly_fields = tuple(field.name for field in DeliveryTask._meta.fields)
    inlines = [DeliveryAttemptInline]

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj=None) -> bool:
        return False


@admin.register(OutboxMessage)
class OutboxMessageAdmin(admin.ModelAdmin):
    # `available_at` — то поле, которое отвечает на вопрос «почему строка не
    # двигается»: она либо прямо сейчас у кого-то в аренде, либо отложена после
    # неудачи. Различает эти два случая соседний `attempts`.
    list_display = ('routing_key', 'exchange', 'created_at', 'available_at', 'published_at', 'attempts')
    list_filter = ('exchange', 'routing_key')
    readonly_fields = tuple(field.name for field in OutboxMessage._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj=None) -> bool:
        return False


@admin.register(NotificationSubscription)
class NotificationSubscriptionAdmin(admin.ModelAdmin):
    list_display = ('subscriber', 'content_id', 'kind', 'last_seen_value', 'last_notification_send', 'is_enabled')
    list_filter = ('kind', 'is_enabled')
    search_fields = ('content_id', 'subscriber__login', 'subscriber__email')
    autocomplete_fields = ('subscriber',)
