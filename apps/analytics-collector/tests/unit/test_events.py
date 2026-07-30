"""Конверт события и запись Kafka (``models/events.py``)."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from practix_analytics_collector.models.enums import EventType
from practix_analytics_collector.models.events import EventContext, EventEnvelope, KafkaRecord


def make_envelope(**overrides) -> EventEnvelope:
    data = {
        'event_id': uuid4(),
        'event_type': EventType.CLICK,
        'received_at': datetime.now(UTC),
        'session_id': 'session-1',
        'context': EventContext(),
    }
    data.update(overrides)
    return EventEnvelope(**data)


class TestPartitionKey:
    """Приоритет user_id → anonymous_id → session_id.

    Порядок не косметический: все события одного пользователя обязаны попадать
    в одну партицию, иначе их нельзя прочитать в порядке записи, а значит
    нельзя восстановить путь по сайту.
    """

    def test_user_id_wins(self):
        user_id = UUID('11111111-1111-1111-1111-111111111111')
        envelope = make_envelope(user_id=user_id, anonymous_id='anon-1', session_id='session-1')
        assert envelope.partition_key() == str(user_id)

    def test_anonymous_id_is_used_when_user_is_unknown(self):
        envelope = make_envelope(anonymous_id='anon-1', session_id='session-1')
        assert envelope.partition_key() == 'anon-1'

    def test_session_id_is_the_last_resort(self):
        envelope = make_envelope(session_id='session-1')
        assert envelope.partition_key() == 'session-1'

    def test_empty_anonymous_id_does_not_shadow_session(self):
        """Пустая строка — не идентификатор: все клиенты с ней слились бы в
        одну партицию и вытеснили бы её собой."""
        envelope = make_envelope(anonymous_id='', session_id='session-1')
        assert envelope.partition_key() == 'session-1'


class TestKafkaRecordRoundTrip:
    """Буфер деградации хранит запись в Redis как JSON и восстанавливает её
    при дренаже. Потеря чего-либо на этом круге = отправка не тех байт."""

    def test_round_trip_preserves_everything(self):
        record = KafkaRecord(
            topic='ugc.clicks.v1',
            key='anon-1',
            value=b'{"event_id": "abc", "payload": {"\\u0442\\u0435\\u043a\\u0441\\u0442": 1}}',
            headers=[('event_type', b'click'), ('schema_version', b'1')],
            attempts=3,
        )

        restored = KafkaRecord.from_dict(record.to_dict())

        assert restored.topic == record.topic
        assert restored.key == record.key
        assert restored.value == record.value
        assert restored.headers == record.headers
        assert restored.attempts == record.attempts

    def test_non_ascii_payload_survives(self):
        """Тело — UTF-8 JSON, и кириллица в нём не должна ломаться о
        сериализацию в Redis."""
        record = KafkaRecord(topic='t', key='k', value='{"название": "Звезда"}'.encode())
        assert KafkaRecord.from_dict(record.to_dict()).value == record.value

    def test_attempts_default_to_zero_for_legacy_entries(self):
        """Записи, попавшие в буфер до появления счётчика попыток, должны
        читаться, а не ронять дренаж."""
        restored = KafkaRecord.from_dict({'topic': 't', 'key': 'k', 'value': '{}'})
        assert restored.attempts == 0
        assert restored.headers == []
