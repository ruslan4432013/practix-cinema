"""Перечисления предметной области.

Все категориальные поля событий типизированы enum'ами, а не свободными
строками. Это даёт три эффекта сразу: (1) валидация на входе отсекает мусор и
инъекции, (2) аналитик получает конечный, документированный словарь значений,
(3) добавление нового значения — явное изменение схемы, а не тихий дрейф данных.
"""

from enum import StrEnum

from practix_contracts.v1.event_types import EventType as _EventType

# EventType переехал в practix_contracts.v1: словарь типов событий существовал в
# двух видах — этот enum и frozenset из шести строк, набранный руками в ETL.
# Реэкспорт, чтобы весь остальной код сервиса не менялся.
EventType = _EventType


class ElementType(StrEnum):
    """Тип элемента интерфейса, по которому кликнул пользователь."""

    FILM = 'film'
    TRAILER = 'trailer'
    CATEGORY = 'category'
    GENRE = 'genre'
    PERSON = 'person'
    BANNER = 'banner'
    BUTTON = 'button'
    LINK = 'link'
    MENU_ITEM = 'menu_item'
    SEARCH_RESULT = 'search_result'
    RECOMMENDATION = 'recommendation'
    OTHER = 'other'


class PageType(StrEnum):
    """Тип просматриваемой страницы."""

    HOME = 'home'
    FILM = 'film'
    GENRE = 'genre'
    PERSON = 'person'
    SEARCH = 'search'
    PROMO = 'promo'
    PROFILE = 'profile'
    PLAYER = 'player'
    OTHER = 'other'


class VideoQuality(StrEnum):
    """Качество видеопотока."""

    Q_144P = '144p'
    Q_240P = '240p'
    Q_360P = '360p'
    Q_480P = '480p'
    Q_720P = '720p'
    Q_1080P = '1080p'
    Q_1440P = '1440p'
    Q_2160P = '2160p'
    AUTO = 'auto'


class SearchFilterField(StrEnum):
    """Поле, по которому пользователь фильтрует поисковую выдачу."""

    GENRE = 'genre'
    RATING = 'rating'
    YEAR = 'year'
    ACTOR = 'actor'
    DIRECTOR = 'director'
    COUNTRY = 'country'
    LANGUAGE = 'language'
    DURATION = 'duration'
    SORT = 'sort'


class DeviceType(StrEnum):
    """Класс устройства (выводится сервером из User-Agent)."""

    DESKTOP = 'desktop'
    MOBILE = 'mobile'
    TABLET = 'tablet'
    TV = 'tv'
    BOT = 'bot'
    UNKNOWN = 'unknown'


class DeliveryStatus(StrEnum):
    """Что сервис сделал с принятым событием (возвращается клиенту)."""

    # Событие передано продюсеру Kafka.
    ACCEPTED = 'accepted'
    # Kafka недоступна — событие лежит в Redis-буфере и будет доставлено дренажом.
    BUFFERED = 'buffered'
    # Событие с таким event_id уже принималось — повторная запись подавлена.
    DUPLICATE = 'duplicate'
    # Ни Kafka, ни Redis недоступны — событие потеряно (зафиксировано метрикой).
    DROPPED = 'dropped'
    # Событие отброшено фильтром до публикации (сейчас — трафик ботов).
    # Отличается от DROPPED принципиально: там авария, здесь штатное решение
    # не класть мусор в топик. Ответ остаётся 202 — клиенту сообщать нечего.
    FILTERED = 'filtered'
