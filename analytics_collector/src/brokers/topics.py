"""Соответствие типа события топику Kafka.

Выбрана стратегия «топик на тип сущности» из теории спринта: отдельные топики
для кликов, просмотров страниц, событий плеера и событий поиска. Единый топик
упростил бы глобальный обзор, но лишил бы возможности масштабировать и
настраивать retention для каждого потока отдельно, а топик на экземпляр
сущности недопустим при такой кардинальности — число топиков росло бы без
предела, и брокеру не хватило бы файловых дескрипторов.

Обоснование числа партиций и сроков хранения — в docs/kafka_topics.md.

БЕЗОПАСНОСТЬ. Имя топика никогда не берётся из запроса: оно резолвится по
серверному enum ``EventType``. Иначе клиент мог бы писать в произвольный топик
кластера — включая служебные — или создавать новые, если у брокера включено
автосоздание топиков.
"""

from core.config import settings
from models.enums import EventType


def topic_for(event_type: EventType) -> str:
    """Возвращает имя топика для типа события."""
    # Словарь строится на каждый вызов, чтобы подхватывать monkeypatch настроек
    # в тестах; операция дешёвая (пять строк) на фоне сетевой отправки.
    mapping = {
        EventType.CLICK: settings.KAFKA_TOPIC_CLICKS,
        EventType.PAGE_VIEW: settings.KAFKA_TOPIC_PAGE_VIEWS,
        # Оба события плеера живут в одном топике: их анализируют совместно
        # (смена качества перед обрывом просмотра — типичный запрос), а общий
        # ключ партиционирования гарантирует их взаимный порядок.
        EventType.VIDEO_QUALITY_CHANGE: settings.KAFKA_TOPIC_VIDEO_EVENTS,
        EventType.VIDEO_COMPLETED: settings.KAFKA_TOPIC_VIDEO_EVENTS,
        # Метки прогресса — поток другого порядка: при тике раз в 30 секунд
        # двухчасовой фильм даёт ~240 событий на просмотр против одного
        # video_completed. В общем топике этот firehose задавил бы редкие, но
        # ценные события плеера — очередь меток задерживала бы чтение
        # досмотров — и заставил бы держать тики те же 30 дней. Разные объёмы
        # и разная ценность означают разные топики.
        EventType.VIDEO_PROGRESS: settings.KAFKA_TOPIC_VIDEO_PROGRESS,
        EventType.SEARCH_FILTER_USED: settings.KAFKA_TOPIC_SEARCH_EVENTS,
    }
    try:
        return mapping[event_type]
    except KeyError:  # pragma: no cover — недостижимо, пока enum и словарь синхронны
        raise ValueError(f'No topic configured for event type {event_type!r}') from None


def all_topics() -> list[str]:
    """Все топики сервиса, включая DLQ (используется ETL и тестами)."""
    return [
        settings.KAFKA_TOPIC_CLICKS,
        settings.KAFKA_TOPIC_PAGE_VIEWS,
        settings.KAFKA_TOPIC_VIDEO_EVENTS,
        settings.KAFKA_TOPIC_VIDEO_PROGRESS,
        settings.KAFKA_TOPIC_SEARCH_EVENTS,
        settings.KAFKA_TOPIC_DLQ,
    ]
