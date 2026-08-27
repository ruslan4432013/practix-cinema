"""Форма шаблона: три проверки теории на пути к кнопке «Сохранить».

Логика проверок живёт в чистом ``services.templating`` — здесь только перевод её
результата на язык форм Django. Так одну и ту же валидацию проходят и шаблон,
сохранённый менеджером, и шаблон, пришедший через API: разойтись они не могут.
"""

from django import forms

from practix_notifications.content.models import MessageTemplate
from practix_notifications.core.config import settings
from practix_notifications.enums import BodyFormat, Channel
from practix_notifications.services.templating import TemplateValidationError, validate_template


class MessageTemplateForm(forms.ModelForm):
    class Meta:
        model = MessageTemplate
        fields = '__all__'

    def clean(self) -> dict:
        cleaned = super().clean()
        allowed = set(cleaned.get('allowed_variables') or [])
        sample = cleaned.get('sample_context') or {}
        is_html = cleaned.get('body_format') == BodyFormat.HTML.value

        if cleaned.get('channel') == Channel.EMAIL.value and not (cleaned.get('subject_template') or '').strip():
            self.add_error('subject_template', 'У письма должна быть тема')

        # Тема проверяется как текст: экранирование в ней превратило бы кавычки в
        # `&quot;` прямо в списке писем у пользователя.
        self._validate_field('subject_template', cleaned.get('subject_template') or '', allowed, sample, is_html=False)
        self._validate_field('body_template', cleaned.get('body_template') or '', allowed, sample, is_html=is_html)
        return cleaned

    def _validate_field(self, field: str, source: str, allowed: set[str], sample: dict, *, is_html: bool) -> None:
        if not source.strip():
            return
        try:
            validate_template(
                source,
                allowed=allowed,
                sample=sample,
                is_html=is_html,
                max_bytes=settings.NOTIFY_TEMPLATE_MAX_BYTES,
                timeout=settings.NOTIFY_RENDER_TIMEOUT_SECONDS,
            )
        except TemplateValidationError as exc:
            # Все причины сразу, а не первая: менеджер должен увидеть весь список
            # за одно сохранение, а не выяснять их по одной.
            for message in exc.errors:
                self.add_error(field, message)
