"""Словарь v1 закреплён ЛИТЕРАЛАМИ — намеренно.

Смысл этих тестов не в проверке кода, а в том, чтобы расширение контракта было
осознанной правкой diff'а, а не побочным эффектом. Схема v1 уже в продакшене:
данные с этими значениями лежат в ClickHouse, а имена топиков заведены в
кластере Kafka. Переименовать что-либо здесь — значит сломать разбор уже
записанных событий, поэтому v1 после выпуска не правится: ломающее изменение
создаёт ``v2/``.

Если тест упал — не «починить тест», а решить, нужна ли новая версия схемы.
"""

from practix_contracts.v1 import ENVELOPE_FIELD_NAMES, REQUIRED_FIELDS, SCHEMA_VERSION
from practix_contracts.v1.event_types import (
    FILM_VIEW_EVENT_TYPES,
    KNOWN_EVENT_TYPES,
    EventType,
)
from practix_contracts.v1.partition_key import partition_key
from practix_contracts.v1.topics import (
    ALL_TOPIC_KEYS,
    DEFAULT_TOPICS,
    EVENT_TYPE_TO_TOPIC_KEY,
    default_topic_names,
)


def test_schema_version_is_one():
    assert SCHEMA_VERSION == 1


def test_event_type_values_are_frozen():
    assert [e.value for e in EventType] == [
        'click',
        'page_view',
        'video_quality_change',
        'video_progress',
        'video_completed',
        'search_filter_used',
    ]


def test_known_event_types_is_derived_not_retyped():
    """Раньше ETL набирал эти шесть строк вручную — расхождение было бы молчаливым."""
    assert {e.value for e in EventType} == KNOWN_EVENT_TYPES


def test_film_view_event_types_exclude_quality_change():
    """Смена качества не означает, что фильм смотрели дальше."""
    assert {'video_progress', 'video_completed'} == FILM_VIEW_EVENT_TYPES
    assert EventType.VIDEO_QUALITY_CHANGE not in FILM_VIEW_EVENT_TYPES


def test_topic_defaults_are_frozen():
    assert DEFAULT_TOPICS == {
        'CLICKS': 'ugc.clicks.v1',
        'PAGE_VIEWS': 'ugc.page_views.v1',
        'VIDEO_EVENTS': 'ugc.video_events.v1',
        'VIDEO_PROGRESS': 'ugc.video_progress.v1',
        'SEARCH_EVENTS': 'ugc.search_events.v1',
        'DLQ': 'ugc.events.dlq.v1',
    }


def test_canonical_topic_order_is_frozen():
    """Порядок дублировался у коллектора и у ETL; теперь он один."""
    assert ALL_TOPIC_KEYS == ('CLICKS', 'PAGE_VIEWS', 'VIDEO_EVENTS', 'VIDEO_PROGRESS', 'SEARCH_EVENTS', 'DLQ')
    assert default_topic_names() == [
        'ugc.clicks.v1',
        'ugc.page_views.v1',
        'ugc.video_events.v1',
        'ugc.video_progress.v1',
        'ugc.search_events.v1',
        'ugc.events.dlq.v1',
    ]


def test_dlq_is_last_and_present():
    assert ALL_TOPIC_KEYS[-1] == 'DLQ'


def test_every_event_type_maps_to_a_topic():
    """Пропущенный тип означал бы ValueError в рантайме при первой публикации."""
    assert set(EVENT_TYPE_TO_TOPIC_KEY) == set(EventType)


def test_every_mapped_topic_key_exists():
    assert set(EVENT_TYPE_TO_TOPIC_KEY.values()) <= set(DEFAULT_TOPICS)


def test_player_events_share_one_topic():
    assert EVENT_TYPE_TO_TOPIC_KEY[EventType.VIDEO_QUALITY_CHANGE] == 'VIDEO_EVENTS'
    assert EVENT_TYPE_TO_TOPIC_KEY[EventType.VIDEO_COMPLETED] == 'VIDEO_EVENTS'


def test_progress_has_its_own_topic():
    """~240 тиков на просмотр против одного video_completed — разные объёмы."""
    assert EVENT_TYPE_TO_TOPIC_KEY[EventType.VIDEO_PROGRESS] == 'VIDEO_PROGRESS'


def test_envelope_fields_are_frozen():
    assert REQUIRED_FIELDS == ('event_id', 'event_type', 'session_id', 'received_at')
    assert set(REQUIRED_FIELDS) <= set(ENVELOPE_FIELD_NAMES)


# --- ключ партиционирования -------------------------------------------------


def test_user_id_wins():
    assert partition_key('u-1', 'anon-1', 'sess-1') == 'u-1'


def test_anonymous_id_is_next():
    assert partition_key(None, 'anon-1', 'sess-1') == 'anon-1'


def test_session_id_is_last_resort():
    assert partition_key(None, None, 'sess-1') == 'sess-1'


def test_all_missing_yields_empty_string():
    assert partition_key(None, None, None) == ''


def test_uuid_user_id_is_stringified():
    import uuid

    uid = uuid.uuid4()
    assert partition_key(uid, None, 'sess') == str(uid)


def test_empty_strings_are_treated_as_absent():
    """Пустая строка не должна становиться ключом партиции."""
    assert partition_key('', '', 'sess') == 'sess'


# --- Третья копия списка топиков: шелл-скрипт создания ----------------------


def test_create_topics_script_defaults_match_the_contract():
    """`create_topics.sh` — третье место, где перечислены те же шесть имён.

    Скрипт запускается внутри контейнера Kafka, где нет ни Python-окружения, ни
    этого пакета, поэтому имена там остаются как fallback-значения
    ``${KAFKA_TOPIC_X:-...}``. Сгенерировать их нельзя — но можно потребовать,
    чтобы они совпадали с контрактом: расхождение означало бы, что кластер
    создаёт топик с одним именем, а коллектор пишет в другой, и брокер молча
    создал бы второй топик (при включённом автосоздании) либо отказал.

    Числа партиций и сроки хранения в скрипте НЕ проверяются: это операционные
    параметры Kafka, а не часть контракта событий.
    """
    import re
    from pathlib import Path

    from practix_contracts.v1.topics import ALL_TOPIC_KEYS, DEFAULT_TOPICS

    repo_root = Path(__file__).resolve().parents[3]
    script = repo_root / 'apps' / 'analytics-collector' / 'scripts' / 'create_topics.sh'
    assert script.exists(), f'скрипт не найден: {script}'

    found = dict(re.findall(r'\$\{KAFKA_TOPIC_([A-Z_]+):-([^}]+)\}', script.read_text()))
    expected = {key: DEFAULT_TOPICS[key] for key in ALL_TOPIC_KEYS}
    assert found == expected, (
        f'Имена топиков в create_topics.sh расходятся с контрактом.\n  в скрипте: {found}\n  в контракте: {expected}'
    )
