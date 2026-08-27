"""Словарь предметной области: каналы, статусы, причины пропуска.

Модуль намеренно чистый — только stdlib. Его импортируют и модели Django, и
конверт сообщения, и отправители, и юнит-тесты, которым Django не нужен вовсе.
Если бы перечисления жили в ``models.py``, разбор конверта в воркере тянул бы за
собой ORM, а тест конверта требовал бы настроенного Django.

``StrEnum`` вместо ``models.TextChoices`` по той же причине: ``TextChoices``
живёт в Django. Метод ``choices()`` отдаёт ровно то, что ждёт поле модели.
"""

from enum import StrEnum


class _Labelled(StrEnum):
    """Перечисление, умеющее отдавать ``choices`` для поля Django."""

    @classmethod
    def choices(cls) -> list[tuple[str, str]]:
        return [(member.value, member.label) for member in cls]

    @property
    def label(self) -> str:
        return _LABELS.get((type(self).__name__, self.value), self.value)


class Channel(_Labelled):
    """Способ доставки.

    Реализованы ``EMAIL`` и ``WEBSOCKET``; ``PUSH`` и ``SMS`` — точки расширения,
    объявленные заранее, потому что требование «система должна иметь возможность
    расширения на смс, push и другие варианты» проверяется на архитектуре, а не
    на наличии пустых классов. Отправители регистрируются в ``channels``.

    Websocket и подтвердил эту проверку на практике: второй канал не потребовал
    ни новой модели, ни правки веера, ни правки админки.
    """

    EMAIL = 'email'
    WEBSOCKET = 'websocket'
    PUSH = 'push'
    SMS = 'sms'


class DomainEvent(_Labelled):
    """Фиксированные события проекта, которые сервис умеет принимать.

    Словарь закрытый: имя события из чужого сервиса — это контракт, и приём
    неизвестного типа обязан быть ошибкой вызывающего, а не молчаливым
    созданием чего-то непонятного. Расширение словаря — видимый диффом факт,
    закреплённый тестом ``test_domain_events``.

    ``film.published``, а не ``film.released``: сигнал в админке наблюдает
    «создалась строка фильма», а не наступление даты релиза. Имя должно
    называть то, что произошло на самом деле.
    """

    USER_REGISTERED = 'user.registered'
    FILM_PUBLISHED = 'film.published'
    #: Служебный тип для сообщений в свободном формате (``POST /messages``).
    #: Здесь он ради того, чтобы у прямой отправки был тот же механизм
    #: разрешения кампании и тот же выключатель, что у фиксированных событий.
    NOTIFICATION_DIRECT = 'notification.direct'


class EventAudience(_Labelled):
    """Кому адресовано письмо по событию.

    Разница принципиальная: ``SUBJECT`` — это один человек, о котором событие
    (зарегистрировался, ему ответили), и такой прогон не двигает статус
    кампании. ``CAMPAIGN`` — обычная массовая рассылка, просто поводом для неё
    стало событие, а не кнопка менеджера.
    """

    SUBJECT = 'subject'
    CAMPAIGN = 'campaign'


class BodyFormat(_Labelled):
    HTML = 'html'
    TEXT = 'text'


class Category(_Labelled):
    """Тип рассылки. Сверяется с отписками пользователя.

    Пользователь может отключить маркетинг, но не транзакционные письма: чек об
    оплате и подтверждение адреса обязаны доходить всегда.
    """

    MARKETING = 'marketing'
    DIGEST = 'digest'
    SYSTEM = 'system'
    TRANSACTIONAL = 'transactional'


#: Категории, от которых отписаться нельзя.
MANDATORY_CATEGORIES = frozenset({Category.TRANSACTIONAL})


class SegmentKind(_Labelled):
    ALL = 'all'
    STATIC = 'static'
    FILTER = 'filter'


class CampaignStatus(_Labelled):
    DRAFT = 'draft'
    SCHEDULED = 'scheduled'
    QUEUED = 'queued'
    RUNNING = 'running'
    DONE = 'done'
    FAILED = 'failed'
    CANCELLED = 'cancelled'


class ScheduleKind(_Labelled):
    IMMEDIATE = 'immediate'
    DEFERRED = 'deferred'
    RECURRING = 'recurring'


class RunStatus(_Labelled):
    PLANNED = 'planned'
    FANNING_OUT = 'fanning_out'
    PUBLISHED = 'published'
    FAILED = 'failed'


class TaskStatus(_Labelled):
    PENDING = 'pending'
    QUEUED = 'queued'
    SENT = 'sent'
    FAILED = 'failed'
    SKIPPED = 'skipped'


class SkipReason(_Labelled):
    OPTED_OUT = 'opted_out'
    NO_ADDRESS = 'no_address'
    INACTIVE = 'inactive'
    #: Событие протухло, пока лежало в очереди: пользователь уже посмотрел серию,
    #: о выходе которой мы собирались его уведомить.
    STALE_EVENT = 'stale_event'
    CHANNEL_UNAVAILABLE = 'channel_unavailable'
    #: Auth не знает такого пользователя. Отдельно от INACTIVE специально:
    #: «отписался» и «его больше нет в системе» требуют от менеджера разных
    #: действий, а от нас — разговора с владельцем витрины подписчиков.
    UNKNOWN_USER = 'unknown_user'


class AttemptResult(_Labelled):
    SENT = 'sent'
    RETRY = 'retry'
    FAILED = 'failed'
    SKIPPED = 'skipped'


_LABELS: dict[tuple[str, str], str] = {
    ('Channel', 'email'): 'Email',
    ('Channel', 'websocket'): 'Websocket (мгновенно в кабинет)',
    ('Channel', 'push'): 'Push (не реализован)',
    ('Channel', 'sms'): 'SMS (не реализован)',
    ('DomainEvent', 'user.registered'): 'Регистрация пользователя',
    ('DomainEvent', 'film.published'): 'Появился новый фильм',
    ('DomainEvent', 'notification.direct'): 'Прямое сообщение (свободный формат)',
    ('EventAudience', 'subject'): 'Только тот, о ком событие',
    ('EventAudience', 'campaign'): 'Аудитория рассылки',
    ('BodyFormat', 'html'): 'HTML',
    ('BodyFormat', 'text'): 'Текст',
    ('Category', 'marketing'): 'Маркетинг',
    ('Category', 'digest'): 'Подборка',
    ('Category', 'system'): 'Системное',
    ('Category', 'transactional'): 'Транзакционное (отписка невозможна)',
    ('SegmentKind', 'all'): 'Все активные подписчики',
    ('SegmentKind', 'static'): 'Фиксированный список',
    ('SegmentKind', 'filter'): 'По условию',
    ('CampaignStatus', 'draft'): 'Черновик',
    ('CampaignStatus', 'scheduled'): 'Запланирована',
    ('CampaignStatus', 'queued'): 'Поставлена в очередь',
    ('CampaignStatus', 'running'): 'Отправляется',
    ('CampaignStatus', 'done'): 'Завершена',
    ('CampaignStatus', 'failed'): 'Ошибка',
    ('CampaignStatus', 'cancelled'): 'Отменена',
    ('ScheduleKind', 'immediate'): 'Сразу',
    ('ScheduleKind', 'deferred'): 'Отложенная',
    ('ScheduleKind', 'recurring'): 'Повторяющаяся',
    ('RunStatus', 'planned'): 'Запланирован',
    ('RunStatus', 'fanning_out'): 'Разворачивается',
    ('RunStatus', 'published'): 'Опубликован',
    ('RunStatus', 'failed'): 'Ошибка',
    ('TaskStatus', 'pending'): 'Ожидает',
    ('TaskStatus', 'queued'): 'В очереди',
    ('TaskStatus', 'sent'): 'Отправлено',
    ('TaskStatus', 'failed'): 'Ошибка',
    ('TaskStatus', 'skipped'): 'Пропущено',
    ('SkipReason', 'opted_out'): 'Отписан',
    ('SkipReason', 'no_address'): 'Нет адреса',
    ('SkipReason', 'inactive'): 'Неактивен',
    ('SkipReason', 'stale_event'): 'Событие протухло',
    ('SkipReason', 'channel_unavailable'): 'Канал не реализован',
    ('SkipReason', 'unknown_user'): 'Нет в Auth',
    ('AttemptResult', 'sent'): 'Отправлено',
    ('AttemptResult', 'retry'): 'Повтор',
    ('AttemptResult', 'failed'): 'Ошибка',
    ('AttemptResult', 'skipped'): 'Пропущено',
}
