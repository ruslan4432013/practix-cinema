"""Соответствие типа события топику (``brokers/topics.py``)."""

import pytest

from practix_analytics_collector.brokers.topics import all_topics, topic_for
from practix_analytics_collector.core.config import settings
from practix_analytics_collector.models.enums import EventType


class TestTopicMapping:
    @pytest.mark.parametrize('event_type', list(EventType))
    def test_every_event_type_has_a_topic(self, event_type):
        """Главная проверка модуля. Enum и словарь маппинга живут в разных
        файлах, и добавление нового типа события без строки в маппинге даёт
        ValueError в рантайме — на живом трафике, а не на ревью."""
        assert topic_for(event_type)

    def test_player_events_share_one_topic(self):
        """Смена качества и досмотр анализируются совместно (например, «смена
        качества перед обрывом просмотра»), поэтому лежат в одном топике с
        общим ключом партиционирования."""
        assert topic_for(EventType.VIDEO_QUALITY_CHANGE) == topic_for(EventType.VIDEO_COMPLETED)

    def test_video_progress_has_its_own_topic(self):
        """Метки прогресса — поток другого порядка: ~240 событий на просмотр
        против одного video_completed. В общем топике этот firehose задержал бы
        чтение редких, но ценных событий плеера."""
        assert topic_for(EventType.VIDEO_PROGRESS) != topic_for(EventType.VIDEO_COMPLETED)

    def test_dlq_is_not_a_destination_for_normal_events(self):
        """В DLQ пишет только дренаж буфера после исчерпания попыток."""
        destinations = {topic_for(event_type) for event_type in EventType}
        assert settings.KAFKA_TOPIC_DLQ not in destinations

    def test_all_topics_covers_every_destination_plus_dlq(self):
        """ETL и скрипт создания топиков читают этот список: пропущенный топик
        означал бы, что события в него никто не читает."""
        expected = {topic_for(event_type) for event_type in EventType} | {settings.KAFKA_TOPIC_DLQ}
        assert set(all_topics()) == expected
