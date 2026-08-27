"""Личный кабинет: свои уведомления и только свои.

Токен набор подписывает сам общим секретом стенда — контейнера Auth здесь нет, и
в этом весь смысл: проверка подписи локальная, сетевого вызова в Auth на пути
пользовательского запроса не существует. Зато отзыв токена проверяется по-настоящему,
в том же Redis, куда его пишет Auth: тест кладёт туда jti руками.
"""

import uuid
from datetime import UTC, datetime, timedelta

from conftest import Mailpit, access_token, cabinet_get, cabinet_post, make_subscriber, post_message, wait_until

from practix_notifications.inbox.models import InboxMessage
from practix_notifications.subscribers.models import ChannelOptout

FEED = '/api/v1/notifications/me/messages'


def _send(subscriber, **overrides) -> None:
    payload = {
        'event_id': str(uuid.uuid4()),
        'user_id': str(subscriber.id),
        'subject': 'Личное уведомление',
        'text': 'Привет, {{ login }}!',
    }
    payload.update(overrides)
    response = post_message(payload)
    assert response.status_code == 202, response.text


def test_feed_shows_a_delivered_message(direct_binding, subscriber):
    _send(subscriber)
    Mailpit.wait_for(count=1)
    wait_until(lambda: InboxMessage.objects.exists(), message='запись ленты не появилась')

    response = cabinet_get(FEED, token=access_token(str(subscriber.id)))

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload['total'] == 1
    item = payload['items'][0]
    assert item['subject'] == 'Личное уведомление'
    assert item['preview'] == 'Привет, ivan!'
    assert item['read_at'] is None


def test_feed_returns_only_my_messages(direct_binding, subscriber):
    other = make_subscriber('petr', 'petr@example.com', first_name='Пётр', last_name='Сидоров')
    _send(subscriber, subject='Моё')
    _send(other, subject='Чужое')
    Mailpit.wait_for(count=2)
    wait_until(lambda: InboxMessage.objects.count() == 2, message='ленты не заполнились')

    response = cabinet_get(FEED, token=access_token(str(subscriber.id)))

    subjects = [item['subject'] for item in response.json()['items']]
    assert subjects == ['Моё']


def test_row_appears_only_after_delivery(direct_binding, subscriber):
    """Лента отвечает на вопрос «о чём вас уведомили», а не «что мы собирались»."""
    token = access_token(str(subscriber.id))
    assert cabinet_get(FEED, token=token).json()['total'] == 0

    _send(subscriber)
    Mailpit.wait_for(count=1)

    wait_until(
        lambda: cabinet_get(FEED, token=token).json()['total'] == 1,
        message='запись ленты не появилась после доставки',
    )


def test_opted_out_message_never_appears_in_the_feed(direct_binding, subscriber):
    """Показывать человеку то, от чего он отписался, хуже, чем не показывать ничего."""
    ChannelOptout.objects.create(subscriber=subscriber, channel='email')

    _send(subscriber)

    wait_until(
        lambda: subscriber.tasks.filter(status='skipped').exists(),
        message='задача не была пропущена по отписке',
    )
    Mailpit.assert_stable(count=0, seconds=3)
    assert cabinet_get(FEED, token=access_token(str(subscriber.id))).json()['total'] == 0


def test_mark_as_read_sets_read_at(direct_binding, subscriber):
    _send(subscriber)
    Mailpit.wait_for(count=1)
    wait_until(lambda: InboxMessage.objects.exists(), message='запись ленты не появилась')

    token = access_token(str(subscriber.id))
    message_id = cabinet_get(FEED, token=token).json()['items'][0]['id']

    response = cabinet_post(f'{FEED}/{message_id}/read', token=token)

    assert response.status_code == 200
    assert cabinet_get(FEED, token=token).json()['items'][0]['read_at'] is not None


def test_pagination_clamps_the_limit(direct_binding, subscriber):
    response = cabinet_get(FEED, token=access_token(str(subscriber.id)), limit=100000)

    assert response.status_code == 200
    assert response.json()['limit'] == 100


def test_missing_token_is_401():
    assert cabinet_get(FEED).status_code == 401


def test_token_signed_with_another_secret_is_401(subscriber):
    forged = access_token(str(subscriber.id), secret='чужой-секрет')

    assert cabinet_get(FEED, token=forged).status_code == 401


def test_expired_token_is_401(subscriber):
    expired = access_token(str(subscriber.id), exp=datetime.now(UTC) - timedelta(hours=1))

    assert cabinet_get(FEED, token=expired).status_code == 401


def test_refresh_token_is_401(subscriber):
    """Refresh живёт неделями: открывать им кабинет — обесценить короткий access."""
    refresh = access_token(str(subscriber.id), token_type='refresh')

    assert cabinet_get(FEED, token=refresh).status_code == 401


def test_revoked_token_is_401(subscriber, auth_redis):
    """Доказывает, что денилист читается в Redis сервиса Auth, а не «нигде»."""
    jti = f'revoked-{uuid.uuid4()}'
    token = access_token(str(subscriber.id), jti=jti)
    assert cabinet_get(FEED, token=token).status_code == 200

    auth_redis.set(jti, 'revoked', ex=60)
    try:
        assert cabinet_get(FEED, token=token).status_code == 401
    finally:
        auth_redis.delete(jti)


def test_unknown_subscriber_gets_an_empty_feed_not_404():
    """У аутентифицированного человека лента существует всегда, просто пуста."""
    response = cabinet_get(FEED, token=access_token(str(uuid.uuid4())))

    assert response.status_code == 200
    assert response.json() == {'items': [], 'total': 0, 'limit': 20, 'offset': 0}
