"""Лента в админке — только чтение.

Это проекция журнала доставки: строка здесь появляется вместе с переводом задачи
в «отправлено». Правка руками рассинхронизировала бы её с журналом, ничего не
починив, — а удалять записи пачкой умеет ``manage purge_inbox``, который знает
про срок хранения.
"""

from django.contrib import admin
from django.http import HttpRequest

from practix_notifications.inbox.models import InboxMessage


@admin.register(InboxMessage)
class InboxMessageAdmin(admin.ModelAdmin):
    list_display = ('subscriber', 'subject', 'channel', 'category', 'sent_at', 'read_at')
    list_filter = ('channel', 'category', 'event_type')
    search_fields = ('subject', 'subscriber__login', 'subscriber__email', 'content_id')
    readonly_fields = tuple(field.name for field in InboxMessage._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj=None) -> bool:
        return False
