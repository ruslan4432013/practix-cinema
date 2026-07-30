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

from practix_analytics_collector.core.config import settings
from practix_analytics_collector.models.enums import EventType
from practix_contracts.v1.topics import ALL_TOPIC_KEYS, EVENT_TYPE_TO_TOPIC_KEY


def _topic_settings() -> dict[str, str]:
    """Ключ топика -> имя из настроек сервиса.

    Имена по-прежнему переопределяются переменными окружения ``KAFKA_TOPIC_*``;
    из контракта берутся канонический ПОРЯДОК и отображение из типа события.
    Словарь строится на каждый вызов, чтобы подхватывать monkeypatch настроек в
    тестах; операция дешёвая на фоне сетевой отправки.
    """
    return {key: getattr(settings, f'KAFKA_TOPIC_{key}') for key in ALL_TOPIC_KEYS}


def topic_for(event_type: EventType) -> str:
    """Возвращает имя топика для типа события."""
    try:
        key = EVENT_TYPE_TO_TOPIC_KEY[event_type]
    except KeyError:  # pragma: no cover — недостижимо, пока enum и контракт синхронны
        raise ValueError(f'No topic configured for event type {event_type!r}') from None
    return _topic_settings()[key]


def all_topics() -> list[str]:
    """Все топики сервиса, включая DLQ (используется ETL и тестами).

    Порядок — канонический из контракта: раньше он дублировался здесь, в
    настройках ETL и в шелл-скрипте создания топиков.
    """
    topics = _topic_settings()
    return [topics[key] for key in ALL_TOPIC_KEYS]
