"""Шаблон сообщения — то, из чего собирается письмо.

Единая шаблонизация и для ручных рассылок менеджера, и для автоматических
событий: требование задания звучит именно так, и выполняется оно тем, что
шаблон один, а источник события — разный.

Отдельной таблицы версий здесь нет намеренно. Воспроизводимость даёт снапшот
темы и тела на КОНКРЕТНОМ прогоне (``campaigns.ScheduledRun``): иначе
повторяющуюся рассылку либо нельзя было бы редактировать, либо она менялась бы
посреди веера — часть получателей со старым текстом, часть с новым. ``revision``
остаётся счётчиком правок для наблюдаемости.
"""

import uuid

from django.db import models

from practix_notifications.enums import BodyFormat, Channel

#: Переменные, доступные менеджеру по умолчанию. Ограничение возможностей
#: шаблонизатора — прямое требование теории: менеджер без опыта разработки
#: способен собрать шаблон, который упадёт при сборке или уйдёт в вечный цикл.
#:
#: ``first_name``/``last_name``/``full_name``/``display_name`` приезжают из Auth
#: в момент сборки письма. Все четыре всегда определены (пустой строкой, если
#: имени нет), а ``full_name`` и ``display_name`` вдобавок откатываются на логин:
#: обращение «Здравствуйте, !» хуже обращения по нику.
DEFAULT_ALLOWED_VARIABLES = [
    'login',
    'email',
    'first_name',
    'last_name',
    'full_name',
    'display_name',
    'unsubscribe_url',
    # Короткая ссылка подтверждения адреса. Её наличие в теле письма — это то,
    # по чему формирующий воркер решает, идти ли в сервис сокращения ссылок:
    # выпускать её всем подряд значило бы заводить строку на каждого получателя
    # любой массовой рассылки.
    'confirm_url',
    'campaign_title',
    'film_title',
    'year',
]

DEFAULT_SAMPLE_CONTEXT = {
    'login': 'ivan',
    'email': 'ivan@example.com',
    'first_name': 'Иван',
    'last_name': 'Петров',
    'full_name': 'Иван Петров',
    'display_name': 'Иван',
    'unsubscribe_url': 'https://practix.local/unsubscribe/demo',
    'confirm_url': 'http://localhost/s/AbC1234',
    'campaign_title': 'Новинки недели',
    'film_title': 'Звёздные войны',
    'year': 2026,
}


class MessageTemplate(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.SlugField(max_length=64, unique=True, verbose_name='Код')
    name = models.CharField(max_length=255, verbose_name='Название')
    description = models.TextField(blank=True, default='', verbose_name='Описание')
    channel = models.CharField(
        max_length=32, choices=Channel.choices(), default=Channel.EMAIL.value, verbose_name='Канал'
    )
    subject_template = models.CharField(max_length=512, blank=True, default='', verbose_name='Тема')
    body_template = models.TextField(verbose_name='Тело')
    # Формат управляет autoescape: у HTML-письма подстановка ника обязана
    # экранироваться, у текстового — обязана НЕ экранироваться, иначе
    # пользователь получит `&quot;` в теле.
    body_format = models.CharField(
        max_length=8, choices=BodyFormat.choices(), default=BodyFormat.HTML.value, verbose_name='Формат'
    )
    allowed_variables = models.JSONField(
        default=list, blank=True, verbose_name='Разрешённые переменные', help_text='Список имён, доступных в шаблоне'
    )
    sample_context = models.JSONField(
        default=dict, blank=True, verbose_name='Пример контекста', help_text='На нём шаблон проверяется при сохранении'
    )
    revision = models.PositiveIntegerField(default=1, verbose_name='Ревизия')
    is_active = models.BooleanField(default=True, verbose_name='Активен')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'message_template'
        verbose_name = 'Шаблон сообщения'
        verbose_name_plural = 'Шаблоны сообщений'
        ordering = ['name']

    def __str__(self) -> str:
        return f'{self.name} ({self.code})'

    @property
    def is_html(self) -> bool:
        return self.body_format == BodyFormat.HTML.value

    @property
    def allowed_variable_set(self) -> set[str]:
        return set(self.allowed_variables or DEFAULT_ALLOWED_VARIABLES)
