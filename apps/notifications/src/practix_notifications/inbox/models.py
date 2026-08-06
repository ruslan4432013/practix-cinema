"""Лента уведомлений пользователя — то, что он видит в личном кабинете.

Отдельное приложение, а не модель в ``campaigns``, потому что аудитория разная:
``campaigns`` — это операторская механика доставки (прогон, задача, попытка,
outbox), журнал для менеджера и дежурного. ``inbox`` — проекция для чтения
конечным пользователем. Граница приложения делает очевидным, что лента
производна и источником истины не является.
"""

import uuid

from django.db import models

from practix_notifications.enums import Category, Channel
from practix_notifications.subscribers.models import Subscriber


class InboxMessage(models.Model):
    """Копия уведомления для личного кабинета. ПРОЕКЦИЯ, не источник истины.

    Пишется в тот же момент и в той же транзакции, что и перевод задачи в
    «отправлено»: строка здесь означает «человека действительно уведомили», а не
    «мы собирались». Запись на вееру показывала бы пользователю сообщения,
    от которых он сам отписался, а также протухшие и отложенные тихими часами —
    то есть врала бы ровно в тех случаях, ради которых лента и заводится.

    Для канала ``websocket`` эта же строка — ДОЛГОВЕЧНАЯ ступень доставки.
    Websocket-шлюз (``apps/notifications-ws/``) не хранит ничего: кадр, пришедший
    в закрытую вкладку, исчезает. Уведомление при этом не теряется ровно потому,
    что здесь оно уже записано, а клиент при каждом переподключении догоняет
    ленту по ``since`` и склеивает её с кадрами по ``task_id``.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscriber = models.ForeignKey(Subscriber, on_delete=models.CASCADE, related_name='inbox', verbose_name='Подписчик')
    # OneToOne, а не ForeignKey: повторная доставка пачки из брокера не может
    # создать вторую строку ленты даже если ранний выход по терминальному
    # статусу когда-нибудь сломают.
    task = models.OneToOneField(
        'campaigns.DeliveryTask', on_delete=models.CASCADE, related_name='inbox_message', verbose_name='Задача доставки'
    )
    campaign = models.ForeignKey(
        'campaigns.Campaign', on_delete=models.CASCADE, related_name='inbox', verbose_name='Рассылка'
    )
    channel = models.CharField(max_length=32, choices=Channel.choices(), verbose_name='Канал')
    category = models.CharField(max_length=32, choices=Category.choices(), verbose_name='Категория')
    subject = models.CharField(max_length=512, blank=True, default='', verbose_name='Тема')
    # ТОЛЬКО превью. Полное тело письма здесь НЕ хранится: рассылка на сто тысяч
    # человек положила бы в базу сто тысяч копий одного и того же HTML. Письмо
    # целиком лежит в почте пользователя, а кабинету нужен список «о чём вас
    # уведомляли», а не второй почтовый клиент.
    preview = models.CharField(max_length=1024, blank=True, default='', verbose_name='Превью')
    event_type = models.CharField(max_length=64, blank=True, default='', verbose_name='Тип события')
    content_id = models.CharField(max_length=255, blank=True, default='', verbose_name='Идентификатор контента')
    sent_at = models.DateTimeField(verbose_name='Отправлено')
    read_at = models.DateTimeField(null=True, blank=True, verbose_name='Прочитано')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'inbox_message'
        verbose_name = 'Уведомление пользователя'
        verbose_name_plural = 'Уведомления пользователей'
        ordering = ['-sent_at']
        indexes = [
            # Единственный запрос, который делает кабинет: лента одного человека
            # от свежих к старым.
            models.Index(fields=['subscriber', '-sent_at'], name='inbox_subscriber_sent_idx'),
        ]

    def __str__(self) -> str:
        return f'{self.subscriber_id}: {self.subject}'
