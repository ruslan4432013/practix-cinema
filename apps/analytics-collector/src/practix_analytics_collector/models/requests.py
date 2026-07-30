"""Входные DTO ingest-API.

Принципы, которым подчинены все модели этого модуля:

* ``extra='forbid'`` — неизвестное поле не игнорируется, а даёт 422. Тихо
  проглоченное поле означало бы, что клиент считает данные отправленными, а
  аналитик их никогда не увидит.
* Ни одна модель не содержит полей ``user_id``, ``is_authenticated``,
  ``received_at``, ``ip``. Они проставляются исключительно сервером, поэтому
  подделать их из браузера физически невозможно — вместе с ``extra='forbid'``
  попытка их передать превращается в 422.
* Все строки ограничены по длине, все категориальные поля — enum'ы, вложенность
  ``properties`` запрещена. Это защита от «bomb»-полезной нагрузки и от
  бесконтрольного роста размера сообщения в Kafka.
* ``event_type`` объявлен как ``Literal`` со значением по умолчанию: на
  специализированных ручках (``/click``) его можно не передавать, а в батче он
  обязателен — по нему работает дискриминация union'а.
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from practix_analytics_collector.core.config import settings
from practix_analytics_collector.models.enums import ElementType, EventType, PageType, SearchFilterField, VideoQuality

# Разумные границы, за которыми значение заведомо является мусором или атакой.
MAX_URL_LENGTH = 2048
MAX_ID_LENGTH = 128
MAX_QUERY_LENGTH = 256
MAX_PROPERTIES_KEYS = 20
MAX_PROPERTY_KEY_LENGTH = 64
MAX_PROPERTY_VALUE_LENGTH = 256
MAX_FILTERS = 20
# Сутки в миллисекундах — потолок для любых длительностей.
MAX_DURATION_MS = 24 * 60 * 60 * 1000
# Допуск на расхождение часов клиента и сервера.
MAX_CLOCK_SKEW_FUTURE = timedelta(hours=1)
MAX_CLOCK_SKEW_PAST = timedelta(days=7)

IdentifierStr = Annotated[str, Field(min_length=1, max_length=MAX_ID_LENGTH)]
UrlStr = Annotated[str, Field(min_length=1, max_length=MAX_URL_LENGTH)]


def _validate_url(value: str | None) -> str | None:
    """Пропускает только http(s)-URL.

    Отсекает ``javascript:``, ``data:`` и прочие схемы: значение попадает в
    хранилище аналитики и рано или поздно будет отрендерено в чьём-нибудь
    дашборде, поэтому санитизировать его нужно на входе.
    """
    if value is None:
        return None
    lowered = value.strip().lower()
    if not (lowered.startswith('http://') or lowered.startswith('https://')):
        raise ValueError('URL must start with http:// or https://')
    return value.strip()


PropertyValue = str | int | float | bool


def _validate_properties(value: dict | None) -> dict | None:
    """Проверяет пользовательский словарь свойств.

    Разрешён только плоский словарь скалярных значений ограниченного размера:
    произвольная вложенность позволила бы прислать глубоко рекурсивную
    структуру и раздуть сообщение в Kafka.
    """
    if value is None:
        return None
    if len(value) > MAX_PROPERTIES_KEYS:
        raise ValueError(f'properties must contain at most {MAX_PROPERTIES_KEYS} keys')
    for key, item in value.items():
        if not isinstance(key, str) or not key or len(key) > MAX_PROPERTY_KEY_LENGTH:
            raise ValueError(f'property key must be a non-empty string up to {MAX_PROPERTY_KEY_LENGTH} chars')
        # bool проверяем раньше int: в Python bool — подкласс int.
        if isinstance(item, bool):
            continue
        if isinstance(item, (int, float)):
            continue
        if isinstance(item, str):
            if len(item) > MAX_PROPERTY_VALUE_LENGTH:
                raise ValueError(f'property "{key}" exceeds {MAX_PROPERTY_VALUE_LENGTH} chars')
            continue
        raise ValueError(f'property "{key}" must be a string, number or boolean (nesting is not allowed)')
    return value


CustomProperties = Annotated[dict[str, PropertyValue], Field(default=None)]


class ClientContext(BaseModel):
    """Контекст страницы, который присылает клиент.

    Всё, что можно получить на сервере (IP, User-Agent), сюда сознательно не
    входит — серверные данные достовернее и не подделываются.
    """

    model_config = ConfigDict(extra='forbid')

    url: UrlStr | None = Field(default=None, description='URL страницы, на которой произошло событие')
    referrer: UrlStr | None = Field(default=None, description='Реферер страницы')
    screen_width: int | None = Field(default=None, ge=0, le=32_768)
    screen_height: int | None = Field(default=None, ge=0, le=32_768)
    viewport_width: int | None = Field(default=None, ge=0, le=32_768)
    viewport_height: int | None = Field(default=None, ge=0, le=32_768)
    locale: str | None = Field(default=None, max_length=32, description='Локаль браузера, например ru-RU')
    timezone: str | None = Field(default=None, max_length=64, description='IANA-таймзона, например Europe/Moscow')

    _check_url = field_validator('url', 'referrer')(_validate_url)


class BaseEventIn(BaseModel):
    """Общая часть любого события."""

    model_config = ConfigDict(extra='forbid')

    event_id: UUID | None = Field(
        default=None,
        description='Идентификатор события для идемпотентности. Если не задан, генерируется сервером.',
    )
    event_timestamp: datetime | None = Field(
        default=None,
        description='Время события по часам клиента. Авторитетным считается серверное received_at.',
    )
    session_id: IdentifierStr = Field(description='Идентификатор пользовательской сессии')
    anonymous_id: IdentifierStr | None = Field(
        default=None,
        description='Стабильный идентификатор браузера. Используется как ключ партиционирования у анонимов.',
    )
    context: ClientContext | None = None
    properties: dict[str, PropertyValue] | None = Field(
        default=None,
        description='Произвольные дополнительные свойства: плоский словарь скалярных значений.',
    )

    _check_properties = field_validator('properties')(_validate_properties)

    @field_validator('event_timestamp')
    @classmethod
    def _check_timestamp(cls, value: datetime | None) -> datetime | None:
        """Отсекает заведомо неверные клиентские отметки времени.

        Часы на клиенте могут быть сбиты, а могут быть подделаны намеренно.
        Значение за пределами разумного окна испортило бы временные ряды в
        аналитике, поэтому такие события отклоняются.
        """
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        now = datetime.now(UTC)
        if value > now + MAX_CLOCK_SKEW_FUTURE:
            raise ValueError('event_timestamp is too far in the future')
        if value < now - MAX_CLOCK_SKEW_PAST:
            raise ValueError('event_timestamp is too far in the past')
        return value


class ClickEventIn(BaseEventIn):
    """Клик по элементу интерфейса (бизнес-требование 1)."""

    event_type: Literal[EventType.CLICK] = EventType.CLICK
    element_type: ElementType = Field(description='Тип элемента, по которому кликнули')
    element_id: str | None = Field(default=None, max_length=MAX_ID_LENGTH, description='DOM-идентификатор элемента')
    element_text: str | None = Field(default=None, max_length=MAX_PROPERTY_VALUE_LENGTH)
    target_id: UUID | None = Field(default=None, description='UUID сущности (фильм, жанр, персона), если применимо')
    page_url: UrlStr | None = None
    position: int | None = Field(default=None, ge=0, le=100_000, description='Порядковый номер элемента в списке')

    _check_page_url = field_validator('page_url')(_validate_url)


class PageViewEventIn(BaseEventIn):
    """Просмотр страницы и время на ней (бизнес-требование 2)."""

    event_type: Literal[EventType.PAGE_VIEW] = EventType.PAGE_VIEW
    page_type: PageType = Field(description='Тип страницы')
    page_url: UrlStr = Field(description='URL просматриваемой страницы')
    page_title: str | None = Field(default=None, max_length=MAX_PROPERTY_VALUE_LENGTH)
    entity_id: UUID | None = Field(default=None, description='UUID сущности страницы (фильма, жанра, персоны)')
    # Присылается при уходе со страницы (обычно через navigator.sendBeacon).
    duration_ms: int | None = Field(
        default=None, ge=0, le=MAX_DURATION_MS, description='Время, проведённое на странице, в миллисекундах'
    )

    _check_page_url = field_validator('page_url')(_validate_url)


class VideoQualityChangeIn(BaseEventIn):
    """Смена качества видео (кастомное событие)."""

    event_type: Literal[EventType.VIDEO_QUALITY_CHANGE] = EventType.VIDEO_QUALITY_CHANGE
    film_id: UUID = Field(description='UUID фильма')
    from_quality: VideoQuality = Field(description='Качество до переключения')
    to_quality: VideoQuality = Field(description='Качество после переключения')
    playback_position_ms: int | None = Field(default=None, ge=0, le=MAX_DURATION_MS)
    is_auto: bool = Field(default=False, description='Переключение выполнено плеером автоматически')


class VideoProgressIn(BaseEventIn):
    """Периодическая метка прогресса просмотра (кастомное событие).

    Плеер шлёт её раз в 30 секунд воспроизведения. Именно по этим меткам
    строится кривая досмотра: у брошенного просмотра события ``video_completed``
    не существует в принципе, и последняя пришедшая метка — единственный способ
    узнать, на каком месте зритель ушёл.

    Поток самый массовый в системе, поэтому у события отдельный топик и
    рекомендация клиенту слать метки пачками через ``/api/v1/events/batch``.
    """

    event_type: Literal[EventType.VIDEO_PROGRESS] = EventType.VIDEO_PROGRESS
    film_id: UUID = Field(description='UUID фильма')
    playback_position_ms: int = Field(ge=0, le=MAX_DURATION_MS, description='Текущая позиция воспроизведения')
    duration_ms: int = Field(ge=1, le=MAX_DURATION_MS, description='Полная длительность контента')
    quality: VideoQuality | None = None
    is_paused: bool = Field(default=False, description='Метка снята на паузе, а не во время воспроизведения')

    @model_validator(mode='after')
    def _position_within_duration(self) -> 'VideoProgressIn':
        """Позиция не может выходить за длительность.

        Проверка сделана через ``model_validator``, а не ``field_validator`` с
        ``info.data``: последний видит только поля, объявленные ВЫШЕ по тексту
        класса, и молча пропустил бы проверку при перестановке полей.

        5 % допуска — на погрешность плеера и округление на клиенте.
        """
        if self.playback_position_ms > self.duration_ms * 1.05:
            raise ValueError('playback_position_ms exceeds duration_ms')
        return self

    @property
    def completion_rate(self) -> float:
        """Доля просмотра в диапазоне 0..1 (считается сервером, не клиентом)."""
        return round(min(self.playback_position_ms / self.duration_ms, 1.0), 4)


class VideoCompletedIn(BaseEventIn):
    """Просмотр видео до конца (кастомное событие)."""

    event_type: Literal[EventType.VIDEO_COMPLETED] = EventType.VIDEO_COMPLETED
    film_id: UUID = Field(description='UUID фильма')
    duration_ms: int = Field(ge=1, le=MAX_DURATION_MS, description='Полная длительность контента')
    watched_ms: int = Field(ge=0, le=MAX_DURATION_MS, description='Сколько фактически просмотрено')
    quality: VideoQuality | None = None

    @model_validator(mode='after')
    def _watched_within_duration(self) -> 'VideoCompletedIn':
        """Просмотрено не может быть кратно больше длительности.

        Проверка сделана через ``model_validator``, а не ``field_validator`` с
        ``info.data``: последний видит только поля, объявленные ВЫШЕ по тексту
        класса, поэтому перестановка ``duration_ms`` и ``watched_ms`` местами
        молча отключила бы проверку — валидатор получил бы пустой ``info.data``
        и вернул значение как есть. Такую поломку не видно ни на ревью, ни в
        тестах, если те не проверяют именно неверные данные.

        Двукратный запас — на перемотки назад и погрешность плеера.
        """
        if self.watched_ms > self.duration_ms * 2:
            raise ValueError('watched_ms is implausibly larger than duration_ms')
        return self

    @property
    def completion_rate(self) -> float:
        """Доля просмотра в диапазоне 0..1 (считается сервером, не клиентом)."""
        return round(min(self.watched_ms / self.duration_ms, 1.0), 4)


class SearchFilterIn(BaseModel):
    """Один применённый фильтр поиска."""

    model_config = ConfigDict(extra='forbid')

    field: SearchFilterField = Field(description='Поле фильтрации')
    value: str = Field(min_length=1, max_length=MAX_PROPERTY_VALUE_LENGTH, description='Значение фильтра')


class SearchFilterUsedIn(BaseEventIn):
    """Использование фильтров поиска (кастомное событие)."""

    event_type: Literal[EventType.SEARCH_FILTER_USED] = EventType.SEARCH_FILTER_USED
    query: str | None = Field(default=None, max_length=MAX_QUERY_LENGTH, description='Поисковый запрос')
    filters: list[SearchFilterIn] = Field(
        default_factory=list, max_length=MAX_FILTERS, description='Список применённых фильтров'
    )
    results_count: int | None = Field(default=None, ge=0, le=10_000_000)


# Кастомные события: дискриминация по event_type, поэтому в /custom поле обязательно.
CustomEventIn = Annotated[
    VideoQualityChangeIn | VideoProgressIn | VideoCompletedIn | SearchFilterUsedIn,
    Field(discriminator='event_type'),
]

# Любое событие — используется в батч-ручке.
AnyEventIn = Annotated[
    ClickEventIn | PageViewEventIn | VideoQualityChangeIn | VideoProgressIn | VideoCompletedIn | SearchFilterUsedIn,
    Field(discriminator='event_type'),
]


class BatchEventsIn(BaseModel):
    """Пакетная отправка событий.

    Основной сценарий — ``navigator.sendBeacon`` при уходе со страницы: браузер
    отдаёт накопленную пачку одним запросом. Верхняя граница размера пачки
    задаётся конфигурацией и проверяется здесь же.
    """

    model_config = ConfigDict(extra='forbid')

    events: list[AnyEventIn] = Field(min_length=1, description='События пачки')

    @field_validator('events')
    @classmethod
    def _check_batch_size(cls, value: list) -> list:
        if len(value) > settings.UGC_MAX_BATCH_SIZE:
            raise ValueError(f'batch must contain at most {settings.UGC_MAX_BATCH_SIZE} events')
        return value
