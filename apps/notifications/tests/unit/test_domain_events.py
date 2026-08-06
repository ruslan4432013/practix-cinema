"""Словарь фиксированных событий заморожен буквально.

Имена событий — контракт с чужими сервисами, которые несут их строковыми
литералами (Auth в коде продюсера, django-admin — своей копией, потому что он вне
workspace). Сравнения здесь буквальные намеренно: производная проверка вида
``set(DomainEvent) == set(REQUIRED_DATA_FIELDS)`` не заметила бы переименования,
а переименование — это тихо сломанный продюсер в другом репозитории.
"""

from datetime import UTC, datetime

import pytest

from practix_notifications.content.models import DEFAULT_ALLOWED_VARIABLES
from practix_notifications.domain_events import (
    INTAKE_SCHEMA_VERSION,
    NATURAL_KEY_FIELD,
    REQUIRED_DATA_FIELDS,
    context_from,
    dedup_key,
    event_run_key,
    missing_fields,
)
from practix_notifications.enums import DomainEvent, EventAudience
from practix_notifications.services.scheduling import manual_run_key


def test_event_vocabulary_is_frozen():
    assert [member.value for member in DomainEvent] == [
        'user.registered',
        'film.published',
        'notification.direct',
    ]


def test_audience_vocabulary_is_frozen():
    assert [member.value for member in EventAudience] == ['subject', 'campaign']


def test_schema_version_is_one():
    assert INTAKE_SCHEMA_VERSION == 1


def test_required_fields_are_frozen():
    assert REQUIRED_DATA_FIELDS == {
        'user.registered': ('user_id', 'email'),
        'film.published': ('film_id', 'title'),
        'notification.direct': ('user_id',),
    }


def test_every_event_declares_required_fields():
    """Тип без описанных полей проходил бы приём вообще без проверок."""
    assert set(REQUIRED_DATA_FIELDS) == {member.value for member in DomainEvent}


def test_natural_keys_are_frozen():
    assert NATURAL_KEY_FIELD == {'user.registered': 'user_id', 'film.published': 'film_id'}


def test_natural_key_is_itself_a_required_field():
    """Иначе ключ дедупликации мог бы оказаться ``None`` на валидном событии."""
    for event_type, field in NATURAL_KEY_FIELD.items():
        assert field in REQUIRED_DATA_FIELDS[event_type]


def test_run_key_format_is_frozen():
    """Смена формата задним числом сделала бы все прошлые события новыми."""
    assert event_run_key('user.registered', 'abc') == 'evt:user.registered:abc'


def test_run_key_namespace_does_not_collide_with_manual_or_schedule_keys():
    manual = manual_run_key('11111111-1111-1111-1111-111111111111', datetime(2026, 8, 5, tzinfo=UTC))
    assert not manual.startswith('evt:')
    assert event_run_key('user.registered', 'x').startswith('evt:')


@pytest.mark.parametrize(
    ('event_type', 'data', 'expected'),
    [
        ('user.registered', {'user_id': 'u1', 'email': 'a@b.c'}, ()),
        ('user.registered', {'user_id': 'u1'}, ('email',)),
        # Пустая строка — это не адрес и не название.
        ('user.registered', {'user_id': 'u1', 'email': ''}, ('email',)),
        ('film.published', {}, ('film_id', 'title')),
        ('notification.direct', {'user_id': 'u1'}, ()),
    ],
)
def test_missing_fields(event_type, data, expected):
    assert missing_fields(event_type, data) == expected


def test_dedup_key_prefers_the_natural_key():
    """Продюсер, перегенерировавший event_id на повторе, не должен слать второе письмо."""
    first = dedup_key('user.registered', {'user_id': 'u1', 'email': 'a@b.c'}, 'event-1')
    second = dedup_key('user.registered', {'user_id': 'u1', 'email': 'a@b.c'}, 'event-2')
    assert first == second == 'u1'


def test_dedup_key_falls_back_to_event_id_where_there_is_no_natural_key():
    """Два одинаковых прямых сообщения — легитимный сценарий, ключ даёт вызывающий."""
    assert dedup_key('notification.direct', {'user_id': 'u1'}, 'event-1') == 'event-1'
    assert dedup_key('notification.direct', {'user_id': 'u1'}, 'event-2') == 'event-2'


def test_film_context_carries_only_whitelisted_variables():
    context = context_from('film.published', {'film_id': 'f1', 'title': 'Дюна', 'year': 2021})
    assert context == {'film_title': 'Дюна', 'year': 2021}
    assert set(context) <= set(DEFAULT_ALLOWED_VARIABLES)


def test_film_context_omits_an_unknown_year():
    assert context_from('film.published', {'film_id': 'f1', 'title': 'Дюна'}) == {'film_title': 'Дюна'}


def test_registration_context_is_empty():
    """login и email кладёт веер персонально на каждого получателя, и они
    побеждают контекст прогона при рендере — второй источник тех же значений
    был бы мёртвым кодом."""
    assert context_from('user.registered', {'user_id': 'u1', 'email': 'a@b.c', 'login': 'ivan'}) == {}
