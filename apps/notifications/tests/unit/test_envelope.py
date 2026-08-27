"""Конверт сообщения: обязательные поля, версия и терпимость к лишнему."""

from datetime import UTC, datetime

import pytest

from practix_notifications.broker import envelope


def make_target(**overrides) -> envelope.TargetRef:
    payload = {'task_id': 't1', 'idempotency_key': 'k1', 'subscriber_id': 's1'}
    payload.update(overrides)
    return envelope.TargetRef(**payload)


def make_prepared(**overrides) -> envelope.PreparedMessage:
    payload = {
        'task_id': 't1',
        'idempotency_key': 'k1',
        'subscriber_id': 's1',
        'address': 'a@b.c',
        'subject': 'Привет, Иван',
        'body': '<p>Иван Петров</p>',
    }
    payload.update(overrides)
    return envelope.PreparedMessage(**payload)


def build_batch() -> dict:
    return envelope.build_notification_requested(
        run_id='r1',
        campaign_id='c1',
        channel='email',
        category='digest',
        content_id='film:42',
        template_code='weekly',
        template_revision=3,
        context={'campaign_title': 'Новинки'},
        valid_until=datetime(2026, 8, 5, 20, tzinfo=UTC),
        targets=[make_target()],
    )


def build_prepared_batch() -> dict:
    return envelope.build_notification_prepared(
        run_id='r1',
        campaign_id='c1',
        channel='email',
        category='digest',
        content_id='film:42',
        valid_until=datetime(2026, 8, 5, 20, tzinfo=UTC),
        recipients=[make_prepared()],
    )


def _keys(value) -> set[str]:
    """Все ключи объекта на всех уровнях вложенности."""
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _keys(item)}
    return set()


def test_required_fields_are_present():
    """Набор закреплён: удаление поля из конверта ломает потребителя молча."""
    body = build_batch()
    for name in envelope.REQUIRED_FIELDS:
        assert name in body


def test_parse_accepts_unknown_fields():
    """Продюсер новее консьюмера — штатная ситуация при поочерёдном деплое.

    Консьюмер, падающий на лишнем поле, превращает её в остановку рассылки.
    """
    body = build_batch()
    body['unknown_future_field'] = {'nested': True}
    assert envelope.parse(body) is body


def test_parse_rejects_newer_major_version():
    body = build_batch()
    body['schema_version'] = envelope.SCHEMA_VERSION + 1
    with pytest.raises(envelope.EnvelopeError):
        envelope.parse(body)


def test_parse_reports_missing_fields():
    body = build_batch()
    del body['run_id']
    with pytest.raises(envelope.EnvelopeError) as exc:
        envelope.parse(body)
    assert 'run_id' in str(exc.value)


def test_requested_batch_carries_no_personal_data():
    """Требование задания, закреплённое тестом.

    В очередь сборки уезжают ТОЛЬКО идентификаторы: ни адреса, ни имени, ни
    таймзоны. Личные данные формирующий воркер запрашивает у Auth сам.
    """
    body = build_batch()
    assert body['targets'] == [{'task_id': 't1', 'idempotency_key': 'k1', 'subscriber_id': 's1'}]
    # Проверяются именно КЛЮЧИ на всех уровнях: значение 'email' в поле channel
    # законно, а поле с таким именем — нет.
    assert _keys(body).isdisjoint({'address', 'email', 'timezone', 'vars', 'first_name', 'last_name'})


def test_prepared_batch_carries_letters_but_not_their_inputs():
    """Обратная сторона: письмо уже собрано, а входные данные рендера не нужны."""
    body = build_prepared_batch()
    assert body['type'] == envelope.EVENT_NOTIFICATION_PREPARED
    assert body['recipients'][0]['subject'] == 'Привет, Иван'
    assert body['recipients'][0]['address'] == 'a@b.c'
    assert 'context' not in body
    assert 'template' not in body


def test_old_consumer_finds_nothing_in_a_build_batch():
    """Смена ключа вместо смены смысла поля.

    Список получателей в `.requested` лежит под `targets`, поэтому потребитель
    предыдущей версии увидит пустой список и ничего не сделает — вместо того
    чтобы разобрать пачку наполовину и отправить письмо без имени.
    """
    assert envelope.parse_recipients(build_batch()) == []


def test_target_roundtrip():
    original = make_target()
    assert envelope.TargetRef.from_dict(original.as_dict()) == original


def test_prepared_roundtrip():
    original = make_prepared()
    assert envelope.PreparedMessage.from_dict(original.as_dict()) == original


def test_incomplete_target_rejected():
    with pytest.raises(envelope.EnvelopeError):
        envelope.TargetRef.from_dict({'task_id': 't'})


def test_prepared_without_address_rejected():
    """Письмо без адреса разобрать можно, но отправить некуда — лучше сразу отказ."""
    with pytest.raises(envelope.EnvelopeError):
        envelope.PreparedMessage.from_dict({'task_id': 't', 'idempotency_key': 'k', 'subscriber_id': 's'})


def test_valid_until_is_parsed_back():
    body = build_batch()
    assert envelope.parse_valid_until(body) == datetime(2026, 8, 5, 20, tzinfo=UTC)


def test_valid_until_survives_the_second_hop():
    """Свежесть события проверяется дважды, поэтому срок едет и в очередь отправки."""
    assert envelope.parse_valid_until(build_prepared_batch()) == datetime(2026, 8, 5, 20, tzinfo=UTC)


def test_broken_valid_until_rejected():
    with pytest.raises(envelope.EnvelopeError):
        envelope.parse_valid_until({'valid_until': 'завтра'})


def test_delivery_report_type_depends_on_failures():
    ok = envelope.build_delivery_report(run_id='r', campaign_id='c', delivered=5, failed=0, skipped=1)
    bad = envelope.build_delivery_report(run_id='r', campaign_id='c', delivered=4, failed=1, skipped=1)
    assert ok['type'] == envelope.EVENT_NOTIFICATION_DELIVERED
    assert bad['type'] == envelope.EVENT_NOTIFICATION_FAILED


def test_campaign_launched_carries_no_recipients():
    """Список получателей собирает веер, а не источник события."""
    body = envelope.build_campaign_launched(run_id='r1', campaign_id='c1')
    assert 'recipients' not in body


def test_event_ids_are_unique():
    assert build_batch()['event_id'] != build_batch()['event_id']
