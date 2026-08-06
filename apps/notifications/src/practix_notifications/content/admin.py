from django.contrib import admin

from practix_notifications.content.forms import MessageTemplateForm
from practix_notifications.content.models import MessageTemplate


@admin.register(MessageTemplate)
class MessageTemplateAdmin(admin.ModelAdmin):
    """CRUD шаблонов — то, ради чего панель написана на Django.

    Валидация висит на форме, а не на модели: она должна срабатывать при
    сохранении менеджером и не мешать миграциям и фикстурам, где текст заведомо
    корректен.
    """

    form = MessageTemplateForm
    list_display = ('name', 'code', 'channel', 'body_format', 'revision', 'is_active', 'updated_at')
    list_filter = ('channel', 'body_format', 'is_active')
    search_fields = ('name', 'code', 'subject_template', 'body_template')
    readonly_fields = ('revision', 'created_at', 'updated_at')
    fieldsets = (
        (None, {'fields': ('code', 'name', 'description', 'channel', 'is_active')}),
        ('Текст', {'fields': ('subject_template', 'body_template', 'body_format')}),
        (
            'Шаблонизатор',
            {
                'fields': ('allowed_variables', 'sample_context'),
                'description': (
                    'Разрешённые переменные — белый список: всё, чего в нём нет, '
                    'шаблон использовать не сможет. Пример контекста — то, на чём '
                    'шаблон проверяется при сохранении.'
                ),
            },
        ),
        ('Служебное', {'fields': ('revision', 'created_at', 'updated_at')}),
    )

    def save_model(self, request, obj, form, change):
        if change:
            # Счётчик правок. Он не восстанавливает старый текст — за
            # воспроизводимость отвечает снапшот на прогоне рассылки, — но
            # позволяет понять по логу, какой ревизией собиралось письмо.
            obj.revision = (obj.revision or 0) + 1
        super().save_model(request, obj, form, change)
