from django import forms
from django.contrib import admin

from practix_notifications.enums import SegmentKind
from practix_notifications.subscribers.models import (
    SEGMENT_FILTER_FIELDS,
    ChannelOptout,
    Segment,
    SegmentMember,
    Subscriber,
)


class ChannelOptoutInline(admin.TabularInline):
    model = ChannelOptout
    extra = 0


@admin.register(Subscriber)
class SubscriberAdmin(admin.ModelAdmin):
    list_display = ('login', 'email', 'timezone', 'locale', 'is_active', 'synced_at')
    list_filter = ('is_active', 'locale', 'timezone')
    # search_fields обязателен: на подписчика ссылаются autocomplete-поля из
    # соседних приложений, и без него они молча перестанут работать.
    search_fields = ('login', 'email', 'id')
    readonly_fields = ('created_at', 'updated_at', 'synced_at', 'source_version')
    inlines = [ChannelOptoutInline]


class SegmentMemberInline(admin.TabularInline):
    model = SegmentMember
    extra = 0
    autocomplete_fields = ('subscriber',)


class SegmentAdminForm(forms.ModelForm):
    """Проверка условия сегмента при сохранении, а не при рассылке.

    Веер применяет только поля из ``SEGMENT_FILTER_FIELDS`` и на любом чужом
    ключе считает аудиторию пустой. Узнать об этом менеджер должен здесь —
    иначе он узнаёт из отчёта «отправлено 0» через полчаса после запуска.
    """

    class Meta:
        model = Segment
        fields = '__all__'

    def clean(self) -> dict:
        cleaned = super().clean()
        if cleaned.get('kind') != SegmentKind.FILTER.value:
            return cleaned

        criteria = cleaned.get('filter') or {}
        if not isinstance(criteria, dict):
            self.add_error('filter', 'Условие должно быть объектом JSON')
            return cleaned
        if not criteria:
            self.add_error('filter', 'У сегмента типа «фильтр» условие не может быть пустым')
            return cleaned

        unknown = sorted(set(criteria) - SEGMENT_FILTER_FIELDS)
        if unknown:
            allowed = ', '.join(sorted(SEGMENT_FILTER_FIELDS))
            self.add_error('filter', f'Недопустимые поля: {", ".join(unknown)}. Разрешены только: {allowed}')
        return cleaned


@admin.register(Segment)
class SegmentAdmin(admin.ModelAdmin):
    form = SegmentAdminForm
    list_display = ('name', 'code', 'kind', 'created_at')
    list_filter = ('kind',)
    search_fields = ('name', 'code')
    inlines = [SegmentMemberInline]


@admin.register(ChannelOptout)
class ChannelOptoutAdmin(admin.ModelAdmin):
    list_display = ('subscriber', 'channel', 'category', 'opted_out_at')
    list_filter = ('channel', 'category')
    search_fields = ('subscriber__login', 'subscriber__email')
    autocomplete_fields = ('subscriber',)
