"""Начальное наполнение стенда: суперпользователь и демо-рассылка.

Идемпотентна — её зовёт one-shot контейнер миграций при каждом подъёме стенда.
Без неё панель поднимается пустой и без входа: Django-суперпользователь здесь не
создаётся ``createsuperuser --no-input``, потому что тот читает свои
``DJANGO_SUPERUSER_*``, а у нас все ключи под префиксом ``NOTIFY_`` — иначе они
столкнулись бы с суперпользователем контентной админки в общем ``.env``.
"""

import logging
from typing import Any

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from practix_notifications.campaigns.models import Campaign, CampaignSchedule, EventBinding
from practix_notifications.content.models import (
    DEFAULT_ALLOWED_VARIABLES,
    DEFAULT_SAMPLE_CONTEXT,
    MessageTemplate,
)
from practix_notifications.core.config import settings
from practix_notifications.enums import Category, Channel, DomainEvent, EventAudience, ScheduleKind
from practix_notifications.subscribers.models import Subscriber

logger = logging.getLogger('notifications.seed')

DEMO_TEMPLATE_CODE = 'weekly-digest'
DEMO_BODY = """<html>
  <body style="font-family: sans-serif">
    <h1>{{ campaign_title }}</h1>
    <p>Привет, {{ login }}!</p>
    <p>На этой неделе в Practix Cinema — «{{ film_title }}» ({{ year }}).</p>
    <p style="color:#888;font-size:12px">
      <a href="{{ unsubscribe_url }}">Отписаться от таких писем</a>
    </p>
  </body>
</html>
"""

WELCOME_TEMPLATE_CODE = 'welcome'
# `{{ confirm_url }}` — не украшение: именно её наличие в теле включает поход
# формирующего воркера в сервис сокращения ссылок (см. building.py). Ссылка
# короткая, потому что письмо открывают с телефона, и подтверждает адрес, потому
# что до неё регистрация считала адрес подтверждённым просто по факту ввода.
WELCOME_BODY = """<html>
  <body style="font-family: sans-serif">
    <h1>Добро пожаловать в Practix Cinema!</h1>
    <p>Привет, {{ login }}! Осталось подтвердить адрес {{ email }}.</p>
    <p>
      <a href="{{ confirm_url }}"
         style="display:inline-block;padding:12px 24px;border-radius:6px;
                background:#3b82f6;color:#fff;text-decoration:none">
        Подтвердить адрес электронной почты
      </a>
    </p>
    <p style="color:#888;font-size:12px">Ссылка действует ограниченное время.</p>
    <p style="color:#888;font-size:12px">
      <a href="{{ unsubscribe_url }}">Отписаться от таких писем</a>
    </p>
  </body>
</html>
"""

FILM_TEMPLATE_CODE = 'film-published'
FILM_BODY = """<html>
  <body style="font-family: sans-serif">
    <h1>Новинка в Practix Cinema</h1>
    <p>Привет, {{ login }}! У нас появился фильм «{{ film_title }}» ({{ year }}).</p>
    <p style="color:#888;font-size:12px">
      <a href="{{ unsubscribe_url }}">Отписаться от таких писем</a>
    </p>
  </body>
</html>
"""

DIRECT_TEMPLATE_CODE = 'direct-default'
DIRECT_BODY = """<html>
  <body style="font-family: sans-serif">
    <p>Привет, {{ login }}!</p>
  </body>
</html>
"""

#: Рассылки, которые запускаются событиями, а не кнопкой. Каждой соответствует
#: строка ``EventBinding`` — единственное место, где имя чужого события
#: встречается с шаблоном, каналом и категорией.
TRIGGER_CAMPAIGNS = (
    {
        'event_type': DomainEvent.USER_REGISTERED.value,
        'name': 'Событие: приветственное письмо',
        'template_code': WELCOME_TEMPLATE_CODE,
        'category': Category.SYSTEM.value,
        'audience': EventAudience.SUBJECT.value,
        # Регистрация мгновенна, а витрина подписчиков синхронизируется
        # периодически: человека в ней ещё нет, и контакт приносит событие.
        'upsert_subscriber': True,
        # Куда увести после подтверждения адреса. Проставляется явно, а не
        # оставляется пустым «до значения из настроек»: задание требует
        # настроенный редирект на главную, и менеджер должен видеть его в панели,
        # а не догадываться, что подставится.
        'confirm_redirect_url': settings.NOTIFY_CONFIRM_REDIRECT_URL,
        'description': 'Запускается событием user.registered из Auth',
    },
    {
        'event_type': DomainEvent.FILM_PUBLISHED.value,
        'name': 'Событие: анонс нового фильма',
        'template_code': FILM_TEMPLATE_CODE,
        'category': Category.MARKETING.value,
        'audience': EventAudience.CAMPAIGN.value,
        'upsert_subscriber': False,
        'description': 'Запускается событием film.published из контентной админки',
    },
    {
        'event_type': DomainEvent.NOTIFICATION_DIRECT.value,
        'name': 'Прямые сообщения',
        'template_code': DIRECT_TEMPLATE_CODE,
        'category': Category.SYSTEM.value,
        'audience': EventAudience.SUBJECT.value,
        'upsert_subscriber': False,
        'description': 'Дом для счётчиков и настроек ручки POST /api/v1/notifications/messages',
    },
)


class Command(BaseCommand):
    help = 'Создаёт суперпользователя панели и демонстрационную рассылку'

    def handle(self, *args: Any, **options: Any) -> None:
        self._ensure_superuser()
        template = self._ensure_template()
        self._ensure_demo_subscriber()
        self._ensure_campaign(template)
        self._ensure_triggers()

    def _ensure_superuser(self) -> None:
        user_model = get_user_model()
        login = settings.NOTIFY_SUPERUSER_LOGIN
        if user_model.objects.filter(username=login).exists():
            self.stdout.write(f'Суперпользователь {login} уже есть')
            return
        user_model.objects.create_superuser(
            username=login,
            email=settings.NOTIFY_SUPERUSER_EMAIL,
            password=settings.NOTIFY_SUPERUSER_PASSWORD,
        )
        self.stdout.write(f'Создан суперпользователь {login}')

    def _ensure_template(self) -> MessageTemplate:
        template, created = MessageTemplate.objects.get_or_create(
            code=DEMO_TEMPLATE_CODE,
            defaults={
                'name': 'Подборка недели',
                'description': 'Демонстрационный шаблон: показывает белый список переменных и ссылку отписки',
                'channel': Channel.EMAIL.value,
                'subject_template': '{{ campaign_title }}: смотрите «{{ film_title }}»',
                'body_template': DEMO_BODY,
                'allowed_variables': DEFAULT_ALLOWED_VARIABLES,
                'sample_context': DEFAULT_SAMPLE_CONTEXT,
            },
        )
        self.stdout.write(('Создан' if created else 'Уже есть') + f' шаблон {template.code}')
        return template

    def _ensure_demo_subscriber(self) -> None:
        if Subscriber.objects.exists():
            return
        # Один демонстрационный получатель, чтобы кнопку «Отправить сейчас» было
        # на ком проверить до первой синхронизации с Auth.
        Subscriber.objects.create(
            login='demo',
            email='demo@practix.local',
            timezone=settings.NOTIFY_DEFAULT_TIMEZONE,
        )
        self.stdout.write('Создан демонстрационный подписчик demo@practix.local')

    def _ensure_campaign(self, template: MessageTemplate) -> None:
        campaign, created = Campaign.objects.get_or_create(
            name='Демо: подборка недели',
            defaults={
                'description': 'Создана автоматически. Нажмите «Отправить сейчас» в списке рассылок.',
                'channel': Channel.EMAIL.value,
                'template': template,
                'category': Category.DIGEST.value,
                'context': {'film_title': 'Звёздные войны', 'year': 2026},
            },
        )
        if created:
            CampaignSchedule.objects.create(campaign=campaign, kind=ScheduleKind.IMMEDIATE.value, is_enabled=False)
        self.stdout.write(('Создана' if created else 'Уже есть') + f' рассылка «{campaign.name}»')

    def _ensure_triggers(self) -> None:
        """Шаблон, рассылка и привязка на каждое фиксированное событие.

        Расписания у триггерных рассылок НЕТ намеренно: их запускает событие
        извне, а не время. Пустое расписание в списке выглядело бы как забытая
        настройка.
        """
        for spec in TRIGGER_CAMPAIGNS:
            template = self._ensure_trigger_template(spec['template_code'])
            campaign, _ = Campaign.objects.get_or_create(
                name=spec['name'],
                defaults={
                    'description': spec['description'],
                    'channel': Channel.EMAIL.value,
                    'template': template,
                    'category': spec['category'],
                    'confirm_redirect_url': spec.get('confirm_redirect_url', ''),
                },
            )
            binding, created = EventBinding.objects.get_or_create(
                event_type=spec['event_type'],
                defaults={
                    'campaign': campaign,
                    'audience': spec['audience'],
                    'upsert_subscriber': spec['upsert_subscriber'],
                    'description': spec['description'],
                },
            )
            self.stdout.write(('Создана' if created else 'Уже есть') + f' привязка {binding.event_type}')

    def _ensure_trigger_template(self, code: str) -> MessageTemplate:
        bodies = {
            WELCOME_TEMPLATE_CODE: ('Добро пожаловать, {{ login }}!', WELCOME_BODY, 'Приветственное письмо'),
            FILM_TEMPLATE_CODE: ('Новинка: «{{ film_title }}»', FILM_BODY, 'Анонс нового фильма'),
            DIRECT_TEMPLATE_CODE: ('Уведомление', DIRECT_BODY, 'Запасной шаблон прямых сообщений'),
        }
        subject, body, name = bodies[code]
        template, _ = MessageTemplate.objects.get_or_create(
            code=code,
            defaults={
                'name': name,
                'description': 'Создан автоматически для события. Текст можно править в панели.',
                'channel': Channel.EMAIL.value,
                'subject_template': subject,
                'body_template': body,
                'allowed_variables': DEFAULT_ALLOWED_VARIABLES,
                'sample_context': DEFAULT_SAMPLE_CONTEXT,
            },
        )
        return template
