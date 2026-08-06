"""Добавить ``confirm_url`` в белые списки уже существующих шаблонов.

Зачем нужна отдельная миграция. ``seed_demo`` заводит шаблоны через
``get_or_create``, то есть на уже поднятом стенде шаблон ``welcome`` сохранит
СТАРЫЙ ``allowed_variables``. Стоит менеджеру (или той же команде на свежем
стенде) вписать в тело ``{{ confirm_url }}``, как валидация шаблона отвергнет
переменную как неразрешённую.

Миграция ТОЛЬКО расширяет белый список. Тела шаблонов она не трогает: их мог
править менеджер, и переписывать чужой текст миграцией — способ потерять работу
человека без единого следа.
"""

from django.db import migrations

VARIABLE = 'confirm_url'


def add_variable(apps, schema_editor):
    MessageTemplate = apps.get_model('content', 'MessageTemplate')
    for template in MessageTemplate.objects.all():
        variables = list(template.allowed_variables or [])
        # Пустой список означает «действует список по умолчанию» (см.
        # ``allowed_variable_set``), а он новую переменную уже содержит.
        if not variables or VARIABLE in variables:
            continue
        variables.append(VARIABLE)
        template.allowed_variables = variables
        template.save(update_fields=['allowed_variables'])


def drop_variable(apps, schema_editor):
    MessageTemplate = apps.get_model('content', 'MessageTemplate')
    for template in MessageTemplate.objects.all():
        variables = list(template.allowed_variables or [])
        if VARIABLE not in variables:
            continue
        template.allowed_variables = [name for name in variables if name != VARIABLE]
        template.save(update_fields=['allowed_variables'])


class Migration(migrations.Migration):
    dependencies = [
        ('content', '0002_channel_labels'),
    ]

    operations = [
        migrations.RunPython(add_variable, drop_variable),
    ]
