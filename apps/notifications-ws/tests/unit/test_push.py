"""Конверт push'а: консьюмер обязан НЕ падать ни на чём.

Продюсер (``practix_notifications.channels.websocket``) обязан упасть на неполном
конверте — иначе он опубликует мусор. Здесь требование обратное: одно кривое
сообщение не должно оборвать AMQP-соединение и вместе с ним все открытые сокеты
реплики. Ровно поэтому две копии конверта, а не общий класс.
"""

import pytest

from practix_notifications_ws.models.push import SCHEMA_VERSION, Frame, NotificationPush, PushError

VALID = {
    'schema_version': SCHEMA_VERSION,
    'user_id': 'user-1',
    'task_id': 'task-1',
    'subject': 'Новый фильм',
    'preview': 'Смотрите Дюну',
    'sent_at': '2026-08-05T10:00:00+00:00',
}


def test_valid_envelope_is_parsed():
    push = NotificationPush.parse(VALID)

    assert push.user_id == 'user-1'
    assert push.task_id == 'task-1'


def test_frame_wraps_the_payload():
    frame = NotificationPush.parse(VALID).to_frame()

    assert frame['type'] == 'notification'
    assert frame['data']['task_id'] == 'task-1'


@pytest.mark.parametrize('raw', ['строка', 42, None, ['список']])
def test_non_object_envelope_is_rejected(raw):
    with pytest.raises(PushError):
        NotificationPush.parse(raw)


def test_unknown_schema_version_is_rejected():
    """Версия и есть связь между двумя копиями конверта: расхождение обязано
    быть отказом с предупреждением, а не разбором «как получится»."""
    with pytest.raises(PushError):
        NotificationPush.parse({**VALID, 'schema_version': SCHEMA_VERSION + 1})


@pytest.mark.parametrize('field', ['user_id', 'task_id'])
def test_empty_identifiers_are_rejected(field):
    """Кадр без адресата некому доставить, кадр без task_id клиент не склеит с
    лентой и покажет дублем."""
    with pytest.raises(PushError):
        NotificationPush.parse({**VALID, field: ''})


def test_missing_identifier_is_rejected():
    with pytest.raises(PushError):
        NotificationPush.parse({'schema_version': SCHEMA_VERSION, 'user_id': 'user-1'})


def test_unknown_fields_are_ignored_not_rejected():
    """Продюсер может добавить поле раньше, чем обновится шлюз, и это НЕ повод
    рвать соединения — в отличие от продюсера, где extra='forbid' уместен."""
    push = NotificationPush.parse({**VALID, 'какое-то_новое_поле': 'значение'})

    assert push.task_id == 'task-1'


def test_envelope_without_version_is_accepted():
    """Отсутствие версии трактуется как текущая: добавление необязательного
    поля не должно требовать её поднятия."""
    raw = {key: value for key, value in VALID.items() if key != 'schema_version'}

    assert NotificationPush.parse(raw).task_id == 'task-1'


def test_service_frames_have_one_shape():
    """Клиент пишет один switch по `type` и не гадает, что ему прислали."""
    assert Frame.hello(user_id='user-1', ping_interval=25)['type'] == 'hello'
    assert Frame.pong()['type'] == 'pong'
    assert Frame.desync(3) == {'type': 'desync', 'dropped': 3}
