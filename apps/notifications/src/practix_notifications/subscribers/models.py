"""Локальная витрина получателей.

ЗА ЧТО ОТВЕЧАЕТ ЭТА ТАБЛИЦА: кого включать в аудиторию, в какой таймзоне у него
ночь и от чего он отписался. То есть за решения о рассылке — их нужно принимать
пачками по 500 человек одним запросом к своей базе, а не походом в чужой сервис
на каждого. Теория предупреждает прямо: «выгружать данные из базы нужно
аккуратно, иначе можно нагрузить базу и другие компоненты сайта начнут
тормозить».

ЗА ЧТО НЕ ОТВЕЧАЕТ: за личность получателя. Имя, фамилия и актуальный адрес
берутся у владельца этих данных — сервиса Auth — в момент сборки письма
(``services.directory``). Полей имени здесь НЕТ НАМЕРЕННО, и добавлять их «чтобы
не ходить в Auth» не нужно: адрес, скопированный сюда сутки назад, может уже
принадлежать другому человеку, а обезличенное письмо — худший исход, чем письмо
на десять минут позже.
"""

import uuid

from django.db import models

from practix_notifications.enums import Category, Channel, SegmentKind

#: Поля, по которым менеджеру разрешено описывать сегмент типа «фильтр».
#:
#: Белый список, а не «пробросить JSON прямо в ``filter(**)``»: последнее дало бы
#: доступ ко всей схеме через связи (``subscriber__tasks__campaign__…``).
#:
#: Живёт в моделях, чтобы форма админки и веер пользовались ОДНИМ списком:
#: разъехавшись, они дали бы условие, которое сохраняется, но не применяется.
SEGMENT_FILTER_FIELDS = frozenset({'locale', 'timezone'})


class Subscriber(models.Model):
    """Получатель. ``id`` совпадает с ``user.id`` в Auth, поэтому синхронизация —
    обычный upsert, а не сопоставление по email (который пользователь меняет)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, verbose_name='ID пользователя')
    login = models.CharField(max_length=255, verbose_name='Логин')
    email = models.EmailField(max_length=255, unique=True, verbose_name='Email')
    # В Auth таймзоны нет. Она нужна здесь: «тихие часы» без таймзоны получателя
    # означают «тихие часы по времени сервера», то есть ничего.
    timezone = models.CharField(max_length=64, default='Europe/Moscow', verbose_name='Таймзона')
    locale = models.CharField(max_length=16, default='ru', verbose_name='Язык')
    is_active = models.BooleanField(default=True, verbose_name='Активен')
    # Версия исходной записи в Auth. Два ответа синхронизации теоретически могут
    # прийти в обратном хронологическом порядке — теория RabbitMQ советует ровно
    # это поле, чтобы понять, какое обновление новее, и не откатить свежие данные.
    source_version = models.DateTimeField(null=True, blank=True, verbose_name='Версия в Auth')
    synced_at = models.DateTimeField(null=True, blank=True, verbose_name='Синхронизирован')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'subscriber'
        verbose_name = 'Подписчик'
        verbose_name_plural = 'Подписчики'
        indexes = [
            models.Index(fields=['is_active'], name='subscriber_active_idx'),
        ]

    def __str__(self) -> str:
        return f'{self.login} <{self.email}>'


class ChannelOptout(models.Model):
    """Отписка. Пустая ``category`` означает «канал целиком».

    Закрывает требование «должна быть возможность настройки уведомлений
    пользователем, в том числе отключение уведомлений». Хранится отдельной
    таблицей, а не флагами в ``Subscriber``: набор категорий будет расти, а
    добавление колонки на каждую — миграция по всей таблице подписчиков.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscriber = models.ForeignKey(
        Subscriber, on_delete=models.CASCADE, related_name='optouts', verbose_name='Подписчик'
    )
    channel = models.CharField(max_length=32, choices=Channel.choices(), verbose_name='Канал')
    # Пустая строка вместо NULL — сознательно. NULL в SQL не равен NULL, поэтому
    # уникальный индекс по (подписчик, канал, категория) пропустил бы сколько
    # угодно копий «отписки от канала целиком». Обойти это можно
    # `nulls_distinct=False`, но он есть только в Postgres, а юнит-тесты идут на
    # SQLite — пустая строка работает одинаково везде.
    category = models.CharField(
        max_length=32, choices=Category.choices(), default='', blank=True, verbose_name='Категория'
    )
    opted_out_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'channel_optout'
        verbose_name = 'Отписка'
        verbose_name_plural = 'Отписки'
        constraints = [
            models.UniqueConstraint(fields=['subscriber', 'channel', 'category'], name='channel_optout_unique'),
        ]

    def __str__(self) -> str:
        return f'{self.subscriber_id}: {self.channel}/{self.category or "*"}'


class Segment(models.Model):
    """Сохранённая аудитория рассылки."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.SlugField(max_length=64, unique=True, verbose_name='Код')
    name = models.CharField(max_length=255, verbose_name='Название')
    kind = models.CharField(
        max_length=16, choices=SegmentKind.choices(), default=SegmentKind.ALL.value, verbose_name='Тип'
    )
    # JSONField базы-агностичный (django.db.models), а не из django.contrib.postgres:
    # юнит-тесты идут на SQLite, и постгресовое поле молча сделало бы их
    # функциональными.
    filter = models.JSONField(default=dict, blank=True, verbose_name='Условие')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'segment'
        verbose_name = 'Сегмент'
        verbose_name_plural = 'Сегменты'

    def __str__(self) -> str:
        return self.name


class SegmentMember(models.Model):
    """Явный участник сегмента (для ``kind='static'``)."""

    id = models.BigAutoField(primary_key=True)
    segment = models.ForeignKey(Segment, on_delete=models.CASCADE, related_name='members')
    subscriber = models.ForeignKey(Subscriber, on_delete=models.CASCADE, related_name='segment_memberships')

    class Meta:
        db_table = 'segment_member'
        verbose_name = 'Участник сегмента'
        verbose_name_plural = 'Участники сегмента'
        constraints = [
            models.UniqueConstraint(fields=['segment', 'subscriber'], name='segment_member_unique'),
        ]
