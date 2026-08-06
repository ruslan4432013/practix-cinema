"""Контракт push-сообщения между воркером нотификаций и шлюзом.

Представление конверта ЗАДУБЛИРОВАНО с
``practix_notifications/channels/websocket.py`` намеренно — по той же причине,
по которой продублирован конверт аналитики между коллектором и ETL
(``docs/monorepo.md``): у продюсера и консьюмера разные требования к ошибке.
Продюсер обязан упасть на неполном конверте — иначе он опубликует мусор;
консьюмер обязан НЕ падать — иначе одно кривое сообщение роняет шлюз со всеми
открытыми сокетами. Общий класс не может вести себя обоими способами.

Связь между копиями держит ``SCHEMA_VERSION``: конверт с неизвестной версией
отбрасывается с предупреждением, а не разбирается «как получится».

## Чего в конверте НЕТ

Тела письма. В ленте кабинета его нет по той же причине (рассылка на сто тысяч
человек — это сто тысяч копий одного HTML), и шлюз не должен быть местом, где
оно всё-таки поедет по сети. Клиент получает тему и превью; за подробностями он
идёт в ленту по ``task_id``.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: Поднимается, когда меняется СМЫСЛ полей. Добавление необязательного поля
#: версию не двигает: старый шлюз его просто не увидит.
SCHEMA_VERSION = 1


class PushError(ValueError):
    """Конверт не разобрать: не тот формат, не та версия, нет обязательных полей."""


class NotificationPush(BaseModel):
    """Уведомление, доставленное пользователю, в виде, пригодном для вкладки."""

    # extra='ignore', а не 'forbid': продюсер может добавить поле раньше, чем
    # обновится шлюз, и это НЕ повод рвать соединения.
    model_config = ConfigDict(extra='ignore')

    #: Кому. Идентификатор подписчика == user.id в Auth.
    user_id: str
    #: Ключ дедупликации между сокетом и лентой. delivery_task.id, а
    #: ``InboxMessage.task`` — OneToOne, поэтому ключ стабилен по построению.
    task_id: str
    subject: str = ''
    preview: str = ''
    channel: str = 'websocket'
    category: str = ''
    event_type: str = ''
    campaign_title: str = ''
    content_id: str = ''
    sent_at: str = ''

    @classmethod
    def parse(cls, raw: Any) -> 'NotificationPush':
        """Разобрать тело AMQP-сообщения. Бросает ``PushError`` на чём угодно кривом."""
        if not isinstance(raw, dict):
            raise PushError(f'push envelope must be an object, got {type(raw).__name__}')
        version = raw.get('schema_version', SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            raise PushError(f'unsupported schema_version {version!r}, expected {SCHEMA_VERSION}')
        try:
            push = cls.model_validate(raw)
        # pydantic бросает своё исключение, а наружу должен уходить один тип:
        # вызывающий ловит PushError и ничего не знает про валидатор.
        except Exception as exc:
            raise PushError(f'malformed push envelope: {exc}') from exc
        if not push.user_id or not push.task_id:
            raise PushError('push envelope requires non-empty user_id and task_id')
        return push

    def to_frame(self) -> dict[str, Any]:
        """Кадр, который уходит в сокет и в ответ long polling'а."""
        return {'type': 'notification', 'data': self.model_dump()}


class Frame:
    """Служебные кадры шлюза. Все они — объект с полем ``type``.

    Одна форма на все кадры, чтобы клиент писал один ``switch`` и не гадал, что
    именно ему прислали.
    """

    @staticmethod
    def hello(*, user_id: str, ping_interval: float) -> dict[str, Any]:
        """Первый кадр после accept: подтверждение, кем шлюз считает клиента."""
        return {'type': 'hello', 'user_id': user_id, 'ping_interval': ping_interval}

    @staticmethod
    def pong() -> dict[str, Any]:
        return {'type': 'pong'}

    @staticmethod
    def desync(dropped: int) -> dict[str, Any]:
        """Буфер соединения переполнен: часть кадров потеряна, догони ленту.

        Явный сигнал вместо молчаливой потери — единственное, что отличает
        «мы отстали» от «уведомлений не было».
        """
        return {'type': 'desync', 'dropped': dropped}


class TicketResponse(BaseModel):
    """Ответ ручки выдачи ticket'а.

    Вместе с ticket'ом отдаётся ПОЛИТИКА ДЕГРАДАЦИИ. Клиент не должен зашивать
    в JavaScript ни адрес шлюза, ни число попыток переподключения: и то и другое
    настраивается в ``.env`` и обязано меняться без пересборки фронтенда.
    """

    ticket: str
    expires_in: int = Field(description='Секунд до протухания ticket’а')
    ws_url: str = Field(description='Полный адрес websocket-ручки, включая ticket')
    poll_url: str = Field(description='Ручка long polling — ступень деградации')
    reconnect_attempts: int = Field(description='Сколько раз пробовать websocket перед переходом на polling')
    reconnect_base_delay: float = Field(description='Базовая пауза экспоненциального отката, секунды')
    poll_timeout: float = Field(description='Сколько шлюз держит long-poll-запрос, секунды')
    probe_interval: float = Field(description='Как часто пробовать вернуться на websocket, секунды')
