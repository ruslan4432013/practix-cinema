"""Куда вести после подтверждения email — настройка кампании.

Задание требует, чтобы конкретное значение ``redirectUrl`` настраивалось в
панели админа. Поле на кампании, а не ключ в её JSON-контексте: опечатка в имени
ключа JSON не падает, а молча ничего не делает.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('campaigns', '0003_channel_labels'),
    ]

    operations = [
        migrations.AddField(
            model_name='campaign',
            name='confirm_redirect_url',
            field=models.URLField(
                blank=True,
                default='',
                help_text='Пусто — значение из NOTIFY_CONFIRM_REDIRECT_URL (главная страница кинотеатра)',
                verbose_name='Куда вести после подтверждения email',
            ),
        ),
    ]
