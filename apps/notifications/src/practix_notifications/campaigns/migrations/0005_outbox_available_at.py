"""Аренда и отсрочка строки outbox — одной колонкой.

До этой правки слив держал ``SELECT FOR UPDATE SKIP LOCKED`` и ``basic_publish``
внутри одной транзакции, то есть удерживал блокировку строки и соединение с
Postgres всё время сетевого разговора с брокером. Теперь публикация идёт ВНЕ
транзакции, а взаимное исключение между репликами планировщика держит отметка
«не трогать раньше»: захватив строку, реплика ставит ``available_at`` в будущее.

Та же колонка несёт отсрочку после неудачи. Без неё потолок ``attempts``
измерялся не в выносливости, а в секундах: слив крутится раз в секунду, и
десятисекундная недоступность брокера навсегда выкидывала исправную строку из
выборки — сбросить счётчик можно было только руками в базе.

Колонка nullable и без значения по умолчанию: в PostgreSQL это правка одних
только метаданных, без переписывания таблицы и без обратной засыпки.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('campaigns', '0004_campaign_confirm_redirect_url'),
    ]

    operations = [
        migrations.AddField(
            model_name='outboxmessage',
            name='available_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
