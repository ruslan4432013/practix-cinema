"""Websocket-шлюз на настоящем стенде: авторизация и деградация на polling.

## Почему набор здесь, а не в apps/notifications-ws

Проверяется ВСЯ цепочка: заявка на приёме → прогон → веер → формирующий воркер →
``WebsocketSender`` → fanout-обменник → реплика шлюза → сокет. Стенд для неё уже
стоит — вместе с Auth, брокером, Redis денилиста и четырьмя контейнерами
нотификаций, — и его обвязка (``conftest.py``, четыреста с лишним строк) описана
ровно один раз. Второй набор означал бы её копию, а порог дублирования в
репозитории жёсткий.

## Что именно доказывается

1. **Посторонний не подключится.** Ни без ticket'а, ни с чужим, ни с уже
   погашенным, ни по отозванному токену. Коды отказа при этом проверяются как
   «любой отказ»: `async_fastapi_jwt_auth` отвечает на неверную подпись 422, а не
   401, и это общий контракт четырёх сервисов, а не особенность шлюза.
2. **Мгновенная доставка работает** и склеивается с лентой по ``task_id``.
3. **Деградация работает.** То же уведомление доходит по long polling'у, а если
   нет и его — лежит в ленте, откуда клиент забирает его по ``since``.
"""

import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
import requests
from conftest import DELIVERY_TIMEOUT, access_token, make_subscriber, post_message, wait_until
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from practix_notifications.enums import Channel
from practix_notifications.inbox.models import InboxMessage

WS_URL = os.getenv('NOTIFICATIONS_WS_URL', 'http://notifications-ws:8000').rstrip('/')
WS_SOCKET_URL = WS_URL.replace('https://', 'wss://').replace('http://', 'ws://')

#: Кадр обязан прийти быстрее письма: он не ждёт ни SMTP, ни его троттлинга.
FRAME_TIMEOUT = 20.0


def issue_ticket(token: str) -> requests.Response:
    return requests.post(
        f'{WS_URL}/api/v1/ws/ticket',
        headers={'Authorization': f'Bearer {token}'} if token else {},
        timeout=10,
    )


@contextmanager
def open_socket(ticket: str):
    with connect(f'{WS_SOCKET_URL}/api/v1/ws?ticket={ticket}', open_timeout=10) as socket:
        hello = json.loads(socket.recv(timeout=10))
        assert hello['type'] == 'hello', hello
        yield socket


def next_notification(socket, *, timeout: float = FRAME_TIMEOUT) -> dict:
    """Первый содержательный кадр. Служебные (ping) пропускаются."""
    deadline = timeout
    while deadline > 0:
        frame = json.loads(socket.recv(timeout=deadline))
        if frame['type'] == 'notification':
            return frame['data']
        deadline -= 1
    raise AssertionError('уведомление не пришло в сокет')


def long_poll(token: str, *, wait: float = 15) -> list[dict]:
    response = requests.get(
        f'{WS_URL}/api/v1/ws/poll',
        headers={'Authorization': f'Bearer {token}'},
        params={'wait': wait},
        timeout=wait + 10,
    )
    assert response.status_code == 200, response.text
    return response.json()['items']


def send_websocket_message(subscriber, **overrides) -> requests.Response:
    payload = {
        'event_id': str(uuid.uuid4()),
        'user_id': str(subscriber.id),
        'type': Channel.WEBSOCKET.value,
        'subject': 'Мгновенное уведомление',
        'text': 'Привет, {{ login }}!',
    }
    payload.update(overrides)
    return post_message(payload)


@pytest.fixture
def user(subscriber):
    """Подписчик плюс подписанный его идентификатором токен."""
    return subscriber, access_token(str(subscriber.id), jti=f'ws-{uuid.uuid4()}')


# --- авторизация -------------------------------------------------------------


def test_ticket_is_issued_for_a_valid_token(user):
    _, token = user

    response = issue_ticket(token)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body['ticket']
    # Вместе с ticket'ом приезжает ПОЛИТИКА ДЕГРАДАЦИИ: клиент не должен зашивать
    # тайминги в JavaScript.
    assert body['reconnect_attempts'] >= 1
    assert body['poll_url'].endswith('/api/v1/ws/poll')


def test_ticket_is_not_issued_without_a_token():
    assert issue_ticket('').status_code == 401


def test_ticket_is_not_issued_for_a_forged_token(subscriber):
    """Отказ проверяется как «любой», и это фиксация факта, а не небрежность.

    `async_fastapi_jwt_auth` отдаёт на неверную подпись `JWTDecodeError` со
    статусом **422**, а не 401, и статус берётся из самой библиотеки общим
    обработчиком `practix_core.jwt.install_exception_handler`. Это контракт,
    разделяемый с Auth, коллектором и UGC API (там он зафиксирован тем же
    способом — `apps/ugc-api/tests/functional/test_ops.py`). Переопределять его
    в одном сервисе значило бы развести четыре сервиса по кодам ответа ради
    одной ручки.

    Что действительно важно и проверяется жёстко: ticket не выдан.
    """
    forged = access_token(str(subscriber.id), secret='not-our-secret')

    response = issue_ticket(forged)

    assert response.status_code in (401, 422)
    assert 'ticket' not in response.text


def test_ticket_is_not_issued_for_a_revoked_token(subscriber, auth_redis):
    """Отзыв читается там же, где он пишется, — в Redis сервиса Auth."""
    jti = f'ws-revoked-{uuid.uuid4()}'
    token = access_token(str(subscriber.id), jti=jti)
    auth_redis.set(jti, 'true', ex=60)

    assert issue_ticket(token).status_code == 401


def test_socket_without_a_ticket_is_rejected():
    """Отказ оформлен ДО accept: посторонний не получает ни байта данных."""
    with pytest.raises(InvalidStatus):
        connect(f'{WS_SOCKET_URL}/api/v1/ws', open_timeout=10)


def test_socket_with_a_made_up_ticket_is_rejected():
    with pytest.raises(InvalidStatus):
        connect(f'{WS_SOCKET_URL}/api/v1/ws?ticket=made-up', open_timeout=10)


def test_ticket_works_exactly_once(user):
    """Перехваченный из access-лога ticket уже погашен тем, кто подключился."""
    _, token = user
    ticket = issue_ticket(token).json()['ticket']

    with open_socket(ticket), pytest.raises(InvalidStatus):
        connect(f'{WS_SOCKET_URL}/api/v1/ws?ticket={ticket}', open_timeout=10)


# --- мгновенная доставка -----------------------------------------------------


def test_notification_arrives_in_the_socket(direct_binding, user):
    subscriber, token = user
    ticket = issue_ticket(token).json()['ticket']

    with open_socket(ticket) as socket:
        assert send_websocket_message(subscriber).status_code == 202

        data = next_notification(socket)

    assert data['subject'] == 'Мгновенное уведомление'
    assert 'Привет, ivan!' in data['preview']
    # Тела письма в кадре нет: рассылка на сто тысяч человек — это сто тысяч
    # копий одного HTML.
    assert 'body' not in data

    # И ровно этот же task_id лежит в ленте: по нему клиент склеивает push
    # с догоном и не показывает дубль.
    row = wait_until(
        lambda: InboxMessage.objects.filter(subscriber_id=subscriber.id).first(),
        message='строка ленты не появилась',
    )
    assert data['task_id'] == str(row.task_id)


def test_socket_never_sees_someone_elses_notification(direct_binding, user):
    """Главное свойство шлюза, и его мало проверять юнит-тестом реестра."""
    _, token = user
    stranger = make_subscriber('petr', 'petr@example.com')
    ticket = issue_ticket(token).json()['ticket']

    with open_socket(ticket) as socket:
        send_websocket_message(stranger)
        # Ждём, пока уведомление ТОЧНО доставлено — иначе тест доказывал бы лишь
        # то, что мы посмотрели раньше времени.
        wait_until(
            lambda: InboxMessage.objects.filter(subscriber_id=stranger.id).exists(),
            message='чужое уведомление не было доставлено',
        )
        with pytest.raises(TimeoutError):
            socket.recv(timeout=3)


# --- деградация --------------------------------------------------------------


def test_long_polling_delivers_the_same_notification(direct_binding, user):
    """Ступень 2: сокета нет, но задержка по-прежнему секунды, а не минуты."""
    subscriber, token = user

    import threading

    items: list[dict] = []

    def poll():
        items.extend(long_poll(token, wait=DELIVERY_TIMEOUT))

    poller = threading.Thread(target=poll)
    poller.start()
    send_websocket_message(subscriber)
    poller.join(timeout=DELIVERY_TIMEOUT + 10)

    notifications = [item for item in items if item['type'] == 'notification']
    assert notifications, 'long polling ничего не отдал'
    assert notifications[0]['data']['subject'] == 'Мгновенное уведомление'


def test_long_polling_returns_empty_on_timeout(user):
    """Пустой ответ — нормальный исход: клиент сразу открывает следующий запрос.

    Ровно этим long polling и отличается от «самого расточительного» способа из
    теории: холостой ответ приходит раз в двадцать секунд, а не десять раз в секунду.
    """
    _, token = user

    assert long_poll(token, wait=2) == []


def test_long_polling_has_a_budget_of_its_own(user):
    """Деградация не должна быть вектором отказа в обслуживании.

    Раньше ``/poll`` не проверял вообще ничего: ни лимита соединений, ни своего.
    Один пользователь держал сколько угодно параллельных запросов, каждый с
    задачей и очередью кадров, — и выедал память процесса. Теперь у поллеров свой
    потолок (на стенде — два на пользователя), отдельный от сокетного: отказ
    здесь никогда не отнимает слот у клиента с живым сокетом, а отказанному
    остаётся ступень ниже — лента кабинета.
    """
    _, token = user
    held = 2  # NOTIFY_WS_MAX_POLLERS_PER_USER на тестовом стенде

    with ThreadPoolExecutor(max_workers=held) as pool:
        waiting = [pool.submit(long_poll, token, wait=10) for _ in range(held)]
        # Запросы должны реально висеть на сервере к моменту третьего.
        time.sleep(2)

        response = requests.get(
            f'{WS_URL}/api/v1/ws/poll',
            headers={'Authorization': f'Bearer {token}'},
            params={'wait': 5},
            timeout=15,
        )

        assert response.status_code == 429, response.text
        # Без Retry-After клиент вернулся бы мгновенно и молотил бы отказами.
        assert int(response.headers['Retry-After']) >= 1
        for future in waiting:
            future.result(timeout=30)


def test_long_polling_requires_a_valid_token(subscriber):
    """Ступень деградации не должна быть дырой в авторизации.

    Код — «любой отказ», по той же причине, что и у выдачи ticket'а выше.
    """
    response = requests.get(
        f'{WS_URL}/api/v1/ws/poll',
        headers={'Authorization': f'Bearer {access_token(str(subscriber.id), secret="not-our-secret")}'},
        params={'wait': 1},
        timeout=15,
    )

    assert response.status_code in (401, 422)
    assert 'items' not in response.text


def test_long_polling_requires_a_token_at_all():
    response = requests.get(f'{WS_URL}/api/v1/ws/poll', params={'wait': 1}, timeout=15)

    # Отсутствие заголовка — это `MissingTokenError`, и вот он как раз 401.
    assert response.status_code == 401


def test_notification_delivered_to_nobody_is_still_in_the_feed(direct_binding, user):
    """Ступень 3 — единственная ДОЛГОВЕЧНАЯ.

    Шлюз не хранит ничего: уведомление, пришедшее когда сокет закрыт, а
    long-poll-запрос ещё не открыт, исчезает бесследно. Не теряется оно ровно
    потому, что лента пишется в одной транзакции с переводом задачи в
    «отправлено», и клиент догоняет её по ``since``.
    """
    subscriber, token = user
    send_websocket_message(subscriber)

    wait_until(
        lambda: InboxMessage.objects.filter(subscriber_id=subscriber.id).exists(),
        message='уведомление не доставлено',
    )

    from conftest import cabinet_get

    response = cabinet_get('/api/v1/notifications/me/messages', token=token, since='2000-01-01T00:00:00+00:00')

    assert response.status_code == 200
    items = response.json()['items']
    assert len(items) == 1
    assert items[0]['channel'] == Channel.WEBSOCKET.value
    # task_id — ключ склейки с кадром сокета; без него клиент показал бы дубль.
    assert items[0]['task_id']


def test_since_cuts_off_what_the_client_already_has(direct_binding, user):
    """Без ``since`` клиент вычитывал бы всю историю на каждом переподключении."""
    subscriber, token = user
    send_websocket_message(subscriber)

    wait_until(
        lambda: InboxMessage.objects.filter(subscriber_id=subscriber.id).exists(),
        message='уведомление не доставлено',
    )

    from conftest import cabinet_get

    everything = cabinet_get('/api/v1/notifications/me/messages', token=token).json()['items']
    newest = everything[0]['sent_at']

    # Граница строгая: клиент присылает sent_at последнего известного ему
    # сообщения, и включающая граница вернула бы его же снова.
    caught_up = cabinet_get('/api/v1/notifications/me/messages', token=token, since=newest).json()

    assert caught_up['items'] == []
