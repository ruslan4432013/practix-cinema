"""Рассылки и всё, что нужно, чтобы доставить их ровно один раз.

Соответствие словарю теории («Генерация автоматических событий»):

===========================  =======================================  ==================================
теория                       ручная рассылка                          автоматическое уведомление
===========================  =======================================  ==================================
``notification_id``          ``Campaign.id``                          ``NotificationSubscription.id``
``content_id``               ``Campaign.content_id``                  ``NotificationSubscription.content_id``
``last_update``              ``Campaign.updated_at``                  ``NotificationSubscription.last_update``
``last_notification_send``   ``Campaign.last_notification_sent_at``   ``NotificationSubscription.last_notification_send``
===========================  =======================================  ==================================

Три уникальных ограничения в этом модуле держат всю надёжность сервиса:

* ``ScheduledRun.run_key`` — генератор событий не может выстрелить одним слотом
  дважды, в том числе после простоя и в том числе из двух реплик планировщика.
* ``DeliveryTask.idempotency_key`` — повторная доставка сообщения из брокера не
  превращается во второе письмо (``at least once`` у брокера + идемпотентный
  обработчик = ``exactly once`` для пользователя).
* ``OutboxMessage`` — сообщение не теряется между коммитом транзакции и
  публикацией в брокер.
"""

import uuid

from django.conf import settings as django_settings
from django.db import models

from practix_notifications.enums import (
    AttemptResult,
    CampaignStatus,
    Category,
    Channel,
    DomainEvent,
    EventAudience,
    RunStatus,
    ScheduleKind,
    SkipReason,
    TaskStatus,
)
from practix_notifications.subscribers.models import Segment, Subscriber


class Campaign(models.Model):
    """Рассылка, созданная менеджером. Это ``notification_id`` из теории."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, verbose_name='Название')
    description = models.TextField(blank=True, default='', verbose_name='Описание')
    channel = models.CharField(
        max_length=32, choices=Channel.choices(), default=Channel.EMAIL.value, verbose_name='Способ доставки'
    )
    template = models.ForeignKey(
        'content.MessageTemplate', on_delete=models.PROTECT, related_name='campaigns', verbose_name='Шаблон'
    )
    segment = models.ForeignKey(
        Segment,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='campaigns',
        verbose_name='Сегмент',
        help_text='Пусто — все активные подписчики',
    )
    # Ключ доступа к данным, о которых рассылка: ID сериала, подборки, акции.
    # По нему автоматический генератор потом отличит «уже уведомляли» от «нет».
    content_id = models.CharField(max_length=255, blank=True, default='', verbose_name='Ключ контента')
    context = models.JSONField(
        default=dict, blank=True, verbose_name='Контекст', help_text='Общие переменные шаблона для всей рассылки'
    )
    category = models.CharField(
        max_length=32, choices=Category.choices(), default=Category.MARKETING.value, verbose_name='Категория'
    )
    # Тот самый «настраиваемый в панели админа redirectUrl»: куда увести
    # человека после того, как он подтвердил адрес по ссылке из письма.
    #
    # Поле, а не ключ в `context`. В JSON опечатка `redirectUrl` вместо
    # `redirect_url` не падает — она просто ничего не делает, и обнаруживается
    # это по жалобе пользователя. URLField валидируется формой админки.
    # Живёт на кампании, потому что кампания — это то, что менеджер и открывает,
    # настраивая приветственное письмо; второго места искать не нужно.
    confirm_redirect_url = models.URLField(
        blank=True,
        default='',
        verbose_name='Куда вести после подтверждения email',
        help_text='Пусто — значение из NOTIFY_CONFIRM_REDIRECT_URL (главная страница кинотеатра)',
    )
    status = models.CharField(
        max_length=16, choices=CampaignStatus.choices(), default=CampaignStatus.DRAFT.value, verbose_name='Статус'
    )
    respect_quiet_hours = models.BooleanField(default=True, verbose_name='Не писать ночью')
    created_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='campaigns',
        verbose_name='Автор',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    #: ``last_update`` из теории.
    updated_at = models.DateTimeField(auto_now=True)
    #: ``last_notification_send`` из теории.
    last_notification_sent_at = models.DateTimeField(null=True, blank=True, verbose_name='Последняя отправка')

    recipients_total = models.PositiveIntegerField(default=0, verbose_name='Получателей')
    sent_count = models.PositiveIntegerField(default=0, verbose_name='Отправлено')
    failed_count = models.PositiveIntegerField(default=0, verbose_name='Ошибок')
    skipped_count = models.PositiveIntegerField(default=0, verbose_name='Пропущено')

    class Meta:
        db_table = 'campaign'
        verbose_name = 'Рассылка'
        verbose_name_plural = 'Рассылки'
        ordering = ['-created_at']
        indexes = [models.Index(fields=['status'], name='campaign_status_idx')]

    def __str__(self) -> str:
        return self.name


class CampaignSchedule(models.Model):
    """Когда рассылку отправлять: сразу, через N часов или по cron."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    campaign = models.OneToOneField(Campaign, on_delete=models.CASCADE, related_name='schedule')
    kind = models.CharField(
        max_length=16, choices=ScheduleKind.choices(), default=ScheduleKind.IMMEDIATE.value, verbose_name='Тип'
    )
    # Абсолютный момент в UTC. «Через N часов» — это виджет формы, а не единица
    # хранения: сохранив «через 3 часа», мы не смогли бы ответить, от какого
    # момента отсчитывать после перезапуска планировщика.
    run_at = models.DateTimeField(null=True, blank=True, verbose_name='Отправить в')
    cron_expression = models.CharField(
        max_length=128,
        blank=True,
        default='',
        verbose_name='Расписание (cron)',
        help_text='Например, «0 12 * * 5» — каждую пятницу в полдень',
    )
    timezone = models.CharField(max_length=64, default='Europe/Moscow', verbose_name='Таймзона расписания')
    starts_at = models.DateTimeField(null=True, blank=True, verbose_name='Действует с')
    ends_at = models.DateTimeField(null=True, blank=True, verbose_name='Действует до')
    max_runs = models.PositiveIntegerField(null=True, blank=True, verbose_name='Максимум запусков')
    # False: после простоя срабатывает только ПОСЛЕДНИЙ пропущенный слот, и
    # next_run_at перескакивает остальные — «в случае простоя генератора после его
    # запуска не должны дублироваться старые и новые события». True: обходятся все
    # пропущенные слоты, каждый ровно раз (за это отвечает run_key).
    catchup = models.BooleanField(default=False, verbose_name='Догонять пропущенные')
    is_enabled = models.BooleanField(default=True, verbose_name='Включено')
    #: Единственное поле, по которому тик планировщика делает выборку.
    next_run_at = models.DateTimeField(null=True, blank=True, db_index=True, verbose_name='Следующий запуск')
    last_run_at = models.DateTimeField(null=True, blank=True, verbose_name='Последний запуск')
    runs_count = models.PositiveIntegerField(default=0, verbose_name='Запусков')

    class Meta:
        db_table = 'campaign_schedule'
        verbose_name = 'Расписание'
        verbose_name_plural = 'Расписания'

    def __str__(self) -> str:
        return f'{self.campaign_id}: {self.kind}'


class ScheduledRun(models.Model):
    """Один запуск рассылки. Единица идемпотентности генератора событий.

    Прогон же несёт и адресность: письмо по событию о КОНКРЕТНОМ человеке — это
    прогон с заполненным ``audience_subscriber``, а не отдельная рассылка на
    одного. Сто тысяч регистраций не должны превратиться в сто тысяч объектов в
    панели менеджера, каждый со своей ссылкой на шаблон.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    schedule = models.ForeignKey(CampaignSchedule, on_delete=models.CASCADE, null=True, blank=True, related_name='runs')
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name='runs')
    # Пусто — аудитория берётся из сегмента рассылки (обычное поведение).
    # Заполнено — адресный прогон: ровно один получатель, и статус рассылки
    # такой прогон не двигает.
    audience_subscriber = models.ForeignKey(
        Subscriber,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='targeted_runs',
        verbose_name='Единственный получатель',
        help_text='Пусто — аудитория рассылки целиком',
    )
    # Переменные, которые принесло событие (название фильма и т.п.). Побеждают
    # контекст рассылки и проигрывают персональным переменным получателя.
    context_overrides = models.JSONField(default=dict, blank=True, verbose_name='Переменные события')
    # Канал, если событие требует не того, что стоит на рассылке (свободный
    # формат позволяет вызывающему выбрать канал явно).
    channel_override = models.CharField(
        max_length=32, choices=Channel.choices(), blank=True, default='', verbose_name='Канал события'
    )
    event_type = models.CharField(max_length=64, blank=True, default='', verbose_name='Тип события')
    # Идентификатор события у продюсера — только для трассировки: ключом
    # дедупликации служит run_key, см. practix_notifications.domain_events.
    event_id = models.CharField(max_length=64, blank=True, default='', verbose_name='ID события')
    # Повторный тик того же слота ловит IntegrityError и молча выходит. Именно это
    # делает требование «после простоя не должны дублироваться старые и новые
    # события» истинным по построению, а не по аккуратности кода.
    run_key = models.CharField(max_length=255, unique=True, verbose_name='Ключ запуска')
    planned_for = models.DateTimeField(verbose_name='Плановое время')
    status = models.CharField(
        max_length=16, choices=RunStatus.choices(), default=RunStatus.PLANNED.value, verbose_name='Статус'
    )
    # Снапшот текста именно на прогоне: редактирование шаблона не переписывает
    # уже запущенную рассылку и не расщепляет её на «до» и «после».
    subject_snapshot = models.CharField(max_length=512, blank=True, default='')
    body_snapshot = models.TextField(blank=True, default='')
    template_revision = models.PositiveIntegerField(default=1)
    is_html = models.BooleanField(default=True)
    tasks_created = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'scheduled_run'
        verbose_name = 'Запуск рассылки'
        verbose_name_plural = 'Запуски рассылок'
        ordering = ['-planned_for']
        indexes = [
            # «Что прилетело по событиям за последний час» — первый вопрос при
            # разборе жалобы на лишнее письмо.
            models.Index(fields=['event_type', '-created_at'], name='scheduled_run_event_idx'),
        ]

    def __str__(self) -> str:
        return self.run_key

    @property
    def is_targeted(self) -> bool:
        """Прогон на одного получателя.

        Свойство, а не проверка по месту: условие читается в вееру, в счётчиках и
        в админке, и три разных способа спросить одно и то же — три места, где
        забудут поправить.
        """
        return self.audience_subscriber_id is not None


class DeliveryTask(models.Model):
    """Одно сообщение одному получателю в рамках одного прогона."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(ScheduledRun, on_delete=models.CASCADE, related_name='tasks')
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name='tasks')
    subscriber = models.ForeignKey(Subscriber, on_delete=models.CASCADE, related_name='tasks')
    channel = models.CharField(max_length=32, choices=Channel.choices())
    # Адрес фиксируется на вееру: пользователь может сменить email, пока пачка
    # лежит в очереди, и письмо должно уйти туда, куда его адресовали.
    address = models.CharField(max_length=255, blank=True, default='')
    # sha256(run:subscriber:channel). Уникальность здесь — то, что превращает
    # `at least once` брокера в `exactly once` для пользователя.
    idempotency_key = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=16, choices=TaskStatus.choices(), default=TaskStatus.PENDING.value)
    skip_reason = models.CharField(max_length=32, choices=SkipReason.choices(), blank=True, default='')
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True, default='')
    queued_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'delivery_task'
        verbose_name = 'Задача доставки'
        verbose_name_plural = 'Задачи доставки'
        indexes = [
            models.Index(fields=['campaign', 'status'], name='delivery_task_camp_stat_idx'),
            models.Index(fields=['status', 'queued_at'], name='delivery_task_stat_queued_idx'),
        ]

    def __str__(self) -> str:
        return f'{self.campaign_id} -> {self.address}'


class DeliveryAttempt(models.Model):
    """История отправки: по строке на попытку."""

    id = models.BigAutoField(primary_key=True)
    task = models.ForeignKey(DeliveryTask, on_delete=models.CASCADE, related_name='delivery_attempts')
    attempt_no = models.PositiveSmallIntegerField()
    result = models.CharField(max_length=16, choices=AttemptResult.choices())
    error = models.TextField(blank=True, default='')
    # Тот же X-Request-Id, что в заголовке сообщения и в логах. По нему цепочка
    # «кнопка в админке → очередь → письмо» сшивается в Kibana одним запросом.
    request_id = models.CharField(max_length=128, blank=True, default='')
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField()

    class Meta:
        db_table = 'delivery_attempt'
        verbose_name = 'Попытка доставки'
        verbose_name_plural = 'Попытки доставки'
        ordering = ['-finished_at']


class OutboxMessage(models.Model):
    """Транзакционный outbox: сообщение, которое ещё не опубликовано в брокер.

    Без него между ``COMMIT`` статуса рассылки и ``basic_publish`` остаётся окно,
    в котором рассылка молча исчезает: статус говорит «в очереди», а в очереди
    ничего нет. Теория формулирует это прямо: «Если отправка сообщения не удалась,
    сообщение лучше записать в какое-нибудь временное хранилище, а через некоторое
    время попытаться переотправить заново».
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    exchange = models.CharField(max_length=128)
    routing_key = models.CharField(max_length=255)
    body = models.JSONField()
    headers = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    #: «Не трогать раньше этого момента»; ``NULL`` — свободна прямо сейчас.
    #:
    #: Одна колонка на две роли, потому что предикат выборки у них общий:
    #:
    #: * АРЕНДА — реплика планировщика ставит её в будущее перед публикацией и
    #:   уходит разговаривать с брокером ВНЕ транзакции. Именно она, а не
    #:   блокировка строки, разводит две реплики: держать ``SELECT FOR UPDATE``
    #:   всё время сетевого разговора значит держать открытую транзакцию и
    #:   занятое соединение из пула столько же;
    #: * ОТСРОЧКА — после неудачной публикации сюда кладётся экспоненциальный
    #:   backoff. Без него потолок попыток измерялся в секундах: слив крутится
    #:   раз в секунду, и короткая недоступность брокера навсегда выкидывала
    #:   исправную строку из выборки.
    #:
    #: Различить роли глазами можно по ``attempts``/``last_error``: у
    #: арендованной строки они не менялись.
    available_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True, default='')

    class Meta:
        db_table = 'outbox_message'
        verbose_name = 'Исходящее сообщение'
        verbose_name_plural = 'Исходящие сообщения'
        ordering = ['created_at']
        indexes = [
            # Частичный индекс: слив читает только неопубликованные, а их всегда
            # единицы на фоне всей истории публикаций. Появившиеся в выборке
            # слива предикаты по `available_at` и `attempts` его не требуют
            # расширять и не попадают в него намеренно: он уже сузил таблицу до
            # единиц строк, там любой доотбор стоит ничего, а `include=` был бы
            # ещё и только-постгресовым — юнит-набор ходит по SQLite.
            models.Index(
                fields=['created_at'],
                name='outbox_unpublished_idx',
                condition=models.Q(published_at__isnull=True),
            ),
        ]


class NotificationSubscription(models.Model):
    """Подписка на автоматические уведомления об изменении контента.

    Задел под генератор автоматических событий, описанный в теории. Здесь он
    объявлен, но не реализован — см. раздел «не сделано» в README сервиса.
    ``last_seen_value`` — то самое «хранить серию №7, пока пользователю не
    отправлено уведомление о №8», ради которого теория предлагает одно поле
    ``SMALLINT``.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscriber = models.ForeignKey(Subscriber, on_delete=models.CASCADE, related_name='content_subscriptions')
    content_id = models.CharField(max_length=255)
    kind = models.CharField(max_length=64)
    last_seen_value = models.PositiveSmallIntegerField(default=0)
    last_update = models.DateTimeField(null=True, blank=True)
    last_notification_send = models.DateTimeField(null=True, blank=True)
    is_enabled = models.BooleanField(default=True)

    class Meta:
        db_table = 'notification_subscription'
        verbose_name = 'Подписка на контент'
        verbose_name_plural = 'Подписки на контент'
        constraints = [
            models.UniqueConstraint(
                fields=['subscriber', 'content_id', 'kind'], name='notification_subscription_unique'
            ),
        ]


class EventBinding(models.Model):
    """Какое фиксированное событие какую рассылку запускает.

    Строка в базе, а не карта в настройках, по четырём причинам, в порядке веса:

    1. Половина связи и так в базе: шаблон — это строка ``MessageTemplate``,
       проверенная формой админки. Карта в настройках ссылалась бы на неё
       строковым кодом и падала бы пятисоткой на приёме события, когда код
       переименуют.
    2. «Каким письмом встречать нового пользователя» — продуктовое решение
       менеджера, а не константа в образе: смена не должна требовать редеплоя.
       Ровно ради этого и существует панель.
    3. ``PROTECT`` не даст удалить рассылку, на которую смотрит живой триггер.
       Настройки такой гарантии не дают вовсе.
    4. Привязка видна там же, где лежит то, на что она указывает.

    Канал, категория, тихие часы и шаблон берутся из связанной рассылки —
    второго места настройки не появляется.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event_type = models.CharField(max_length=64, unique=True, choices=DomainEvent.choices(), verbose_name='Событие')
    campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name='event_bindings', verbose_name='Рассылка'
    )
    audience = models.CharField(
        max_length=16,
        choices=EventAudience.choices(),
        default=EventAudience.SUBJECT.value,
        verbose_name='Аудитория',
    )
    # Только для регистрации: витрина подписчиков синхронизируется периодически,
    # а письмо нужно сейчас — событие несёт контакт с собой.
    upsert_subscriber = models.BooleanField(default=False, verbose_name='Заводить подписчика из события')
    is_enabled = models.BooleanField(default=True, verbose_name='Включено')
    description = models.TextField(blank=True, default='', verbose_name='Комментарий')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'event_binding'
        verbose_name = 'Привязка события'
        verbose_name_plural = 'Привязки событий'
        ordering = ['event_type']

    def __str__(self) -> str:
        return f'{self.event_type} -> {self.campaign_id}'
