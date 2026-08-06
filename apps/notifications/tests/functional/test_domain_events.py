"""Фиксированные события от чужих сервисов — на настоящем стенде.

Проверяется то, чего не видно в юнит-тестах: что заявка, принятая ручкой,
действительно проходит весь путь — прогон, outbox, публикация планировщиком,
веер, воркер, SMTP — и что повтор запроса продюсером НЕ приводит ко второму
письму. Последнее и есть главное свойство: продюсер ретраит по построению.
"""

import uuid

import pytest
from conftest import Mailpit, make_subscriber, post_event, register_user, wait_until

from practix_notifications.campaigns.models import DeliveryTask, ScheduledRun
from practix_notifications.enums import DomainEvent
from practix_notifications.subscribers.models import Subscriber

FILM_ID = '22222222-2222-2222-2222-222222222222'


@pytest.fixture
def new_user() -> dict:
    """Пользователь, только что заведённый в Auth.

    Событие о регистрации обязано ссылаться на НАСТОЯЩИЙ идентификатор: за
    именем для приветственного письма формирующий воркер пойдёт в Auth, и
    выдуманный id закончился бы пропуском `unknown_user`.
    """
    return register_user('ivan', 'ivan@example.com')


def _registration(user: dict, email: str | None = None) -> dict:
    return {
        'type': DomainEvent.USER_REGISTERED.value,
        'event_id': str(uuid.uuid4()),
        'occurred_at': '2026-08-05T10:00:00+00:00',
        'data': {'user_id': user['id'], 'login': user['login'], 'email': email or user['email']},
    }


def test_user_registered_sends_exactly_one_welcome_letter(welcome_binding, new_user):
    """Одно и то же событие, присланное дважды, даёт ровно одно письмо."""
    first = post_event(_registration(new_user))
    second = post_event(_registration(new_user))

    assert first.status_code == 202, first.text
    assert second.status_code == 200, second.text
    assert second.json() == {'status': 'duplicate', 'run_id': first.json()['run_id']}

    messages = Mailpit.wait_for(count=1)
    assert messages[0]['To'][0]['Address'] == 'ivan@example.com'
    # Отрицательное утверждение нельзя проверить мгновенно: даём системе шанс
    # ошибиться. Второе письмо, если бы оно шло, уже успело бы прийти.
    Mailpit.assert_stable(count=1, seconds=5)
    assert ScheduledRun.objects.count() == 1


def test_user_registered_creates_the_subscriber(welcome_binding, new_user):
    """Витрина синхронизируется периодически, а письмо нужно сейчас."""
    assert not Subscriber.objects.filter(pk=new_user['id']).exists()

    assert post_event(_registration(new_user)).status_code == 202

    Mailpit.wait_for(count=1)
    subscriber = Subscriber.objects.get(pk=new_user['id'])
    assert subscriber.email == 'ivan@example.com'
    assert subscriber.login == 'ivan'


def test_targeted_run_does_not_touch_the_other_subscribers(welcome_binding, new_user):
    """Адресное письмо уходит одному, а не всей аудитории рассылки."""
    make_subscriber('petr', 'petr@example.com', first_name='Пётр', last_name='Сидоров')
    make_subscriber('anna', 'anna@example.com', first_name='Анна', last_name='Кузнецова')

    assert post_event(_registration(new_user)).status_code == 202

    messages = Mailpit.wait_for(count=1)
    Mailpit.assert_stable(count=1, seconds=4)
    assert {message['To'][0]['Address'] for message in messages} == {'ivan@example.com'}


def test_recycled_email_is_ignored_not_five_hundred(welcome_binding, subscriber):
    """Тот же адрес под другим user_id: витрина удалений в Auth не знает.

    Повтор запроса это не вылечит, поэтому 200 «проигнорировано», а не 5xx, —
    иначе продюсер ретраил бы вечно.
    """
    other = register_user('petr', 'petr@example.com', first_name='Пётр', last_name='Сидоров')
    response = post_event(_registration(other, email=subscriber.email))

    assert response.status_code == 200
    assert response.json()['reason'] == 'email_conflict'
    assert ScheduledRun.objects.count() == 0
    Mailpit.assert_stable(count=0, seconds=3)


def test_film_published_reaches_every_active_subscriber(film_binding):
    for index in range(3):
        make_subscriber(f'user{index}', f'user{index}@example.com')

    response = post_event(
        {
            'type': DomainEvent.FILM_PUBLISHED.value,
            'event_id': str(uuid.uuid4()),
            'data': {'film_id': FILM_ID, 'title': 'Дюна', 'year': 2021},
        }
    )

    assert response.status_code == 202, response.text
    messages = Mailpit.wait_for(count=3)
    assert len({message['To'][0]['Address'] for message in messages}) == 3
    # Переменная события дошла до рендера, а не потерялась между веером и воркером.
    body = Mailpit.message(messages[0]['ID'])
    assert 'Дюна' in (body.get('HTML') or '') + (body.get('Text') or '')


def test_same_film_is_announced_once(film_binding):
    make_subscriber('ivan', 'ivan@example.com')
    payload = {
        'type': DomainEvent.FILM_PUBLISHED.value,
        'data': {'film_id': FILM_ID, 'title': 'Дюна', 'year': 2021},
    }

    assert post_event(payload).status_code == 202
    Mailpit.wait_for(count=1)
    assert post_event({**payload, 'event_id': str(uuid.uuid4())}).status_code == 200

    Mailpit.assert_stable(count=1, seconds=5)


def test_unknown_event_type_is_rejected_without_a_run(welcome_binding, new_user):
    response = post_event({'type': 'user.deleted', 'data': {'user_id': new_user['id']}})

    assert response.status_code == 400
    assert ScheduledRun.objects.count() == 0
    Mailpit.assert_stable(count=0, seconds=3)


def test_disabled_binding_is_ignored_not_retried(welcome_binding, new_user):
    """200 «проигнорировано»: продюсер, ретраящий не-2xx, крутился бы вечно."""
    welcome_binding.is_enabled = False
    welcome_binding.save(update_fields=['is_enabled'])

    response = post_event(_registration(new_user))

    assert response.status_code == 200
    assert response.json()['reason'] == 'binding_disabled'
    assert ScheduledRun.objects.count() == 0
    Mailpit.assert_stable(count=0, seconds=3)


def test_event_without_a_binding_is_ignored(new_user):
    response = post_event(_registration(new_user))

    assert response.status_code == 200
    assert response.json()['reason'] == 'no_binding'
    Mailpit.assert_stable(count=0, seconds=3)


def test_wrong_token_is_rejected(welcome_binding, new_user):
    response = post_event(_registration(new_user), token='wrong-secret')

    assert response.status_code == 401
    assert ScheduledRun.objects.count() == 0


def test_legacy_campaign_id_payload_still_works(campaign, subscriber):
    """Исходный контракт ручки: скрипты, написанные против него, не сломаны."""
    response = post_event({'campaign_id': str(campaign.id)})

    assert response.status_code == 202, response.text
    assert 'run_id' in response.json()
    Mailpit.wait_for(count=1)


def test_empty_body_is_still_a_four_hundred():
    response = post_event({})

    assert response.status_code == 400
    assert 'campaign_id or type' in response.json()['detail']


def test_delivery_task_records_the_event(welcome_binding, new_user):
    post_event(_registration(new_user))
    Mailpit.wait_for(count=1)

    task = wait_until(
        lambda: DeliveryTask.objects.filter(status='sent').first(),
        message='задача доставки не перешла в «отправлено»',
    )
    assert task.run.event_type == DomainEvent.USER_REGISTERED.value
    assert task.run.is_targeted
