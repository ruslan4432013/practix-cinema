"""Сообщения в свободном формате на настоящем стенде.

Ручка принимает заявку и кладёт её в очередь — рассылкой она не занимается, как и
ручка событий. Здесь это видно буквально: ответ приходит мгновенно, а письмо
появляется в Mailpit позже, когда до него дойдут планировщик, веер и воркер.
"""

import uuid

from conftest import Mailpit, post_message, wait_until

from practix_notifications.campaigns.models import DeliveryTask, ScheduledRun
from practix_notifications.enums import Channel, SkipReason, TaskStatus


def _payload(subscriber, **overrides) -> dict:
    payload = {
        'event_id': str(uuid.uuid4()),
        'user_id': str(subscriber.id),
        'type': Channel.EMAIL.value,
        'subject': 'Личное уведомление',
        'text': 'Привет, {{ login }}!',
    }
    payload.update(overrides)
    return payload


def test_raw_text_message_reaches_one_user(direct_binding, subscriber):
    response = post_message(_payload(subscriber))

    assert response.status_code == 202, response.text
    messages = Mailpit.wait_for(count=1)
    assert messages[0]['To'][0]['Address'] == subscriber.email
    assert messages[0]['Subject'] == 'Личное уведомление'
    body = Mailpit.message(messages[0]['ID'])
    # Персональная переменная подставлена, и текст остался текстом: autoescape на
    # плоском теле положил бы в письмо HTML-сущности.
    assert 'Привет, ivan!' in (body.get('Text') or '')


def test_template_message_uses_the_template_text(direct_binding, subscriber, template):
    response = post_message(_payload(subscriber, subject=None, text=None, template_id=template.code))

    assert response.status_code == 202, response.text
    messages = Mailpit.wait_for(count=1)
    # Шаблон обращается по имени, а имя приезжает из Auth.
    assert messages[0]['Subject'] == 'Привет, Иван'


def test_idempotency_header_is_present(direct_binding, subscriber):
    post_message(_payload(subscriber))

    messages = Mailpit.wait_for(count=1)
    headers = Mailpit.headers(messages[0]['ID'])
    # По этому заголовку видно, что письмо ушло ровно один раз, а не просто
    # «писем оказалось столько, сколько ожидали».
    assert headers.get('X-Idempotency-Key')


def test_same_event_id_twice_gives_one_letter(direct_binding, subscriber):
    payload = _payload(subscriber)

    first = post_message(payload)
    second = post_message(payload)

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()['status'] == 'duplicate'
    Mailpit.wait_for(count=1)
    Mailpit.assert_stable(count=1, seconds=5)


def test_invalid_raw_template_is_rejected_at_intake(direct_binding, subscriber):
    """Вызывающий — сервис: о сломанном шаблоне он должен узнать кодом ответа,
    а не из dead-letters через три перехода."""
    response = post_message(_payload(subscriber, text='Ваш баланс: {{ balance }}'))

    assert response.status_code == 400
    assert response.json()['errors']
    assert ScheduledRun.objects.count() == 0
    Mailpit.assert_stable(count=0, seconds=3)


def test_sms_is_accepted_and_skipped_as_channel_unavailable(direct_binding, subscriber):
    """Канал объявлен, но не реализован: заявку принимаем, причину пишем в журнал."""
    response = post_message(_payload(subscriber, type=Channel.SMS.value))

    assert response.status_code == 202, response.text
    task = wait_until(
        lambda: DeliveryTask.objects.filter(status=TaskStatus.SKIPPED.value).first(),
        message='задача не была пропущена',
    )
    # Не `no_address`: у нереализованного канала адреса нет по построению, и
    # такая причина отправила бы менеджера искать проблему с контактами.
    assert task.skip_reason == SkipReason.CHANNEL_UNAVAILABLE.value
    assert task.channel == Channel.SMS.value
    Mailpit.assert_stable(count=0, seconds=3)


def test_unknown_subscriber_is_ignored(direct_binding):
    response = post_message(
        {
            'event_id': str(uuid.uuid4()),
            'user_id': '33333333-3333-3333-3333-333333333333',
            'text': 'Привет',
        }
    )

    assert response.status_code == 200
    assert response.json()['reason'] == 'unknown_subscriber'
    Mailpit.assert_stable(count=0, seconds=3)


def test_missing_event_id_is_rejected(direct_binding, subscriber):
    response = post_message({'user_id': str(subscriber.id), 'text': 'Привет'})

    assert response.status_code == 400
    assert ScheduledRun.objects.count() == 0


def test_wrong_token_is_rejected(direct_binding, subscriber):
    response = post_message(_payload(subscriber), token='wrong-secret')

    assert response.status_code == 401
    assert ScheduledRun.objects.count() == 0
