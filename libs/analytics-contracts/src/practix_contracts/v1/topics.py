"""Топики Kafka версии 1: канонический порядок, дефолты и отображение из типа.

Список топиков существовал в ТРЁХ местах: ``brokers/topics.py::all_topics()`` у
коллектора, свойство ``settings.topics`` у ETL и шелл-скрипт создания топиков.
Все три перечисляли одни и те же шесть имён в одном и том же порядке — то есть
любое добавление топика требовало трёх согласованных правок, а пропуск одной из
них означал бы, что ETL не читает топик, который коллектор уже пишет.

Имена по-прежнему переопределяются переменными окружения у каждого сервиса
(``KAFKA_TOPIC_*``); здесь живут ДЕФОЛТЫ и — что важнее — канонический ПОРЯДОК и
отображение «тип события -> ключ топика».

БЕЗОПАСНОСТЬ. Имя топика никогда не берётся из запроса: оно резолвится по
серверному ``EventType``. Иначе клиент мог бы писать в произвольный топик
кластера — включая служебные — или создавать новые, если у брокера включено
автосоздание.
"""

from collections.abc import Mapping

from practix_contracts.v1.event_types import EventType

# Ключ -> имя топика по умолчанию. Ключ совпадает с суффиксом переменной
# окружения: KAFKA_TOPIC_<KEY>.
DEFAULT_TOPICS: Mapping[str, str] = {
    'CLICKS': 'ugc.clicks.v1',
    'PAGE_VIEWS': 'ugc.page_views.v1',
    'VIDEO_EVENTS': 'ugc.video_events.v1',
    'VIDEO_PROGRESS': 'ugc.video_progress.v1',
    'SEARCH_EVENTS': 'ugc.search_events.v1',
    'DLQ': 'ugc.events.dlq.v1',
}

# Канонический порядок. Именно он раньше дублировался: и коллектор, и ETL
# перечисляли топики в этой последовательности, включая DLQ последним.
#
# DLQ включён намеренно: события, не доехавшие с первой попытки, — такие же
# данные, и терять их в хранилище было бы странно. Исходный топик такого
# сообщения виден в заголовке x-original-topic.
ALL_TOPIC_KEYS: tuple[str, ...] = (
    'CLICKS',
    'PAGE_VIEWS',
    'VIDEO_EVENTS',
    'VIDEO_PROGRESS',
    'SEARCH_EVENTS',
    'DLQ',
)

# Тип события -> ключ топика.
EVENT_TYPE_TO_TOPIC_KEY: Mapping[EventType, str] = {
    EventType.CLICK: 'CLICKS',
    EventType.PAGE_VIEW: 'PAGE_VIEWS',
    # Оба события плеера живут в одном топике: их анализируют совместно (смена
    # качества перед обрывом просмотра — типичный запрос), а общий ключ
    # партиционирования гарантирует их взаимный порядок.
    EventType.VIDEO_QUALITY_CHANGE: 'VIDEO_EVENTS',
    EventType.VIDEO_COMPLETED: 'VIDEO_EVENTS',
    # Метки прогресса — поток другого порядка: при тике раз в 30 секунд
    # двухчасовой фильм даёт ~240 событий на просмотр против одного
    # video_completed. В общем топике этот firehose задавил бы редкие, но ценные
    # события плеера и заставил бы держать тики те же 30 дней. Разные объёмы и
    # разная ценность означают разные топики.
    EventType.VIDEO_PROGRESS: 'VIDEO_PROGRESS',
    EventType.SEARCH_FILTER_USED: 'SEARCH_EVENTS',
}


def default_topic_names() -> list[str]:
    """Дефолтные имена всех топиков в каноническом порядке."""
    return [DEFAULT_TOPICS[key] for key in ALL_TOPIC_KEYS]


if __name__ == '__main__':  # pragma: no cover
    # Источник списка для scripts/create_topics.sh: раньше он перечислял топики
    # в четвёртый раз, теперь спрашивает у контракта.
    print('\n'.join(default_topic_names()))
