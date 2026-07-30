"""Словарь типов событий аналитики, версия 1 — ЕДИНСТВЕННЫЙ источник истины.

До вынесения словарь существовал в двух видах: ``EventType(StrEnum)`` у
коллектора и ``KNOWN_EVENT_TYPES = frozenset({...})`` у ETL — те же шесть строк,
набранные руками второй раз. Расходились бы они молча: коллектор начал бы
публиковать новый тип, ETL сложил бы его в ``ugc.invalid_events`` с причиной
``unknown_event_type``, и обнаружилось бы это не падением, а отсутствием данных
в дашборде.

Здесь enum объявлен ОДИН раз, а множество известных типов ВЫВОДИТСЯ из него.
"""

from enum import StrEnum


class EventType(StrEnum):
    """Тип пользовательского события."""

    CLICK = 'click'
    PAGE_VIEW = 'page_view'
    VIDEO_QUALITY_CHANGE = 'video_quality_change'
    # Порядок значений повторяет жизненный цикл просмотра: тики прогресса идут
    # всё время воспроизведения, «досмотрел» приходит один раз в конце.
    VIDEO_PROGRESS = 'video_progress'
    VIDEO_COMPLETED = 'video_completed'
    SEARCH_FILTER_USED = 'search_filter_used'


# Выводится из enum, а не набирается заново — ровно то, что раньше расходилось.
KNOWN_EVENT_TYPES: frozenset[str] = frozenset(EventType)

# Типы, из которых собирается таблица просмотров.
#
# video_quality_change сюда НЕ входит, хотя тоже относится к фильму: смена
# качества не говорит о том, что фильм смотрели дальше, и попав в таблицу
# просмотров, она завысила бы их число.
FILM_VIEW_EVENT_TYPES: frozenset[str] = frozenset(
    {
        EventType.VIDEO_PROGRESS,
        EventType.VIDEO_COMPLETED,
    }
)
