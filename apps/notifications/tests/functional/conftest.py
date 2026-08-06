"""Окружение функционального набора.

Тесты идут против НАСТОЯЩИХ Postgres, RabbitMQ, Mailpit и Auth и против
настоящих контейнеров формирующего воркера, отправляющего воркера и
планировщика. Проверяется ровно то, что нельзя проверить заглушками: что
топология объявлена как задумано, что повторная доставка не даёт второго письма,
что отложенная рассылка действительно уходит позже, что неразбираемое сообщение
оказывается в dead-letters — и что имя в письме приехало из Auth, а не из
локальной таблицы.

Получатели поэтому заводятся НАСТОЯЩЕЙ регистрацией в Auth: подписчик,
существующий только в витрине, для формирующего воркера — «unknown_user», и ни
одного письма по нему не уйдёт. Это не неудобство набора, а проверяемое
поведение.

Django здесь используется как библиотека доступа к той же базе, что и у воркера:
тест создаёт рассылку, а доставляет её отдельный процесс в соседнем контейнере.
"""

import json
import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import django
import jwt
import pika
import pytest
import redis
import requests

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'practix_notifications.settings')
django.setup()

from practix_notifications.broker import topology  # noqa: E402 - только после django.setup()
from practix_notifications.campaigns.models import (  # noqa: E402
    Campaign,
    CampaignSchedule,
    DeliveryAttempt,
    DeliveryTask,
    EventBinding,
    OutboxMessage,
    ScheduledRun,
)
from practix_notifications.content.models import MessageTemplate  # noqa: E402
from practix_notifications.core.config import settings as app_settings  # noqa: E402
from practix_notifications.enums import Channel, DomainEvent, EventAudience  # noqa: E402
from practix_notifications.inbox.models import InboxMessage  # noqa: E402
from practix_notifications.subscribers.models import (  # noqa: E402
    ChannelOptout,
    Segment,
    SegmentMember,
    Subscriber,
)

MAILPIT_URL = os.getenv('MAILPIT_URL', 'http://mailpit:8025').rstrip('/')
RABBITMQ_API_URL = os.getenv('RABBITMQ_API_URL', 'http://rabbitmq:15672').rstrip('/')
RABBITMQ_AUTH = (os.getenv('RABBITMQ_USER', 'guest'), os.getenv('RABBITMQ_PASSWORD', 'guest'))
NOTIFICATIONS_URL = os.getenv('NOTIFICATIONS_URL', 'http://notifications-admin:8000').rstrip('/')
AUTH_URL = os.getenv('AUTH_URL', 'http://auth:8000').rstrip('/')

#: Потолок ожидания доставки. Щедрый намеренно: тик планировщика, слив outbox,
#: веер и отправка — четыре асинхронных шага, и жёсткий таймаут дал бы мигающий
#: набор вместо полезного сигнала.
DELIVERY_TIMEOUT = 30.0


class Mailpit:
    """Клиент HTTP API почтового приёмника."""

    @staticmethod
    def clear() -> None:
        requests.delete(f'{MAILPIT_URL}/api/v1/messages', timeout=5)

    @staticmethod
    def messages() -> list[dict[str, Any]]:
        response = requests.get(f'{MAILPIT_URL}/api/v1/messages', params={'limit': 200}, timeout=5)
        response.raise_for_status()
        return response.json().get('messages', [])

    @staticmethod
    def message(message_id: str) -> dict[str, Any]:
        response = requests.get(f'{MAILPIT_URL}/api/v1/message/{message_id}', timeout=5)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def headers(message_id: str) -> dict[str, list[str]]:
        response = requests.get(f'{MAILPIT_URL}/api/v1/message/{message_id}/headers', timeout=5)
        response.raise_for_status()
        return response.json()

    @classmethod
    def wait_for(cls, *, count: int = 1, timeout: float = DELIVERY_TIMEOUT) -> list[dict[str, Any]]:
        """Дождаться прихода писем. Возвращает список сообщений."""
        deadline = time.monotonic() + timeout
        messages: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            messages = cls.messages()
            if len(messages) >= count:
                return messages
            time.sleep(0.3)
        raise AssertionError(f'Ожидали {count} писем, за {timeout} с пришло {len(messages)}')

    @classmethod
    def assert_stable(cls, *, count: int, seconds: float) -> None:
        """Убедиться, что число писем НЕ изменилось за указанное время.

        Отрицательное утверждение («второго письма не будет») невозможно
        проверить мгновенно: надо дать системе шанс ошибиться.
        """
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            actual = len(cls.messages())
            assert actual == count, f'Ожидали ровно {count} писем, стало {actual}'
            time.sleep(0.3)


class Rabbit:
    """Клиент management API брокера."""

    @staticmethod
    def queue(name: str) -> dict[str, Any]:
        response = requests.get(f'{RABBITMQ_API_URL}/api/queues/%2F/{name}', auth=RABBITMQ_AUTH, timeout=5)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def exchanges() -> list[dict[str, Any]]:
        response = requests.get(f'{RABBITMQ_API_URL}/api/exchanges/%2F', auth=RABBITMQ_AUTH, timeout=5)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def bindings(queue: str) -> list[dict[str, Any]]:
        response = requests.get(f'{RABBITMQ_API_URL}/api/queues/%2F/{queue}/bindings', auth=RABBITMQ_AUTH, timeout=5)
        response.raise_for_status()
        return response.json()

    @classmethod
    def wait_for_depth(cls, name: str, *, at_least: int = 1, timeout: float = DELIVERY_TIMEOUT) -> int:
        deadline = time.monotonic() + timeout
        depth = 0
        while time.monotonic() < deadline:
            depth = cls.queue(name).get('messages', 0)
            if depth >= at_least:
                return depth
            time.sleep(0.3)
        raise AssertionError(f'В очереди {name} ожидали ≥{at_least} сообщений, за {timeout} с накопилось {depth}')

    @staticmethod
    def peek(name: str) -> dict[str, Any] | None:
        """Заглянуть в голову очереди, не вычерпывая её.

        ``ackmode=reject_requeue_true`` возвращает сообщение на место, поэтому
        соседние тесты, считающие глубину, ничего не замечают.
        """
        response = requests.post(
            f'{RABBITMQ_API_URL}/api/queues/%2F/{name}/get',
            auth=RABBITMQ_AUTH,
            json={'count': 1, 'ackmode': 'reject_requeue_true', 'encoding': 'auto'},
            timeout=5,
        )
        response.raise_for_status()
        messages = response.json()
        return messages[0] if messages else None

    @staticmethod
    def purge(name: str) -> None:
        requests.delete(f'{RABBITMQ_API_URL}/api/queues/%2F/{name}/contents', auth=RABBITMQ_AUTH, timeout=5)


@pytest.fixture(scope='session')
def broker():
    """Отдельное соединение тестов с брокером — для публикации сырых сообщений."""
    parameters = pika.URLParameters(app_settings.NOTIFY_AMQP_URL)
    parameters.heartbeat = 60
    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()
    channel.confirm_delivery()
    topology.declare_topology(
        channel,
        retry_ttl_ms=app_settings.NOTIFY_RETRY_TTL_MS,
        build_retry_ttl_ms=app_settings.NOTIFY_RETRY_BUILD_TTL_MS,
    )
    yield channel
    connection.close()


@pytest.fixture(autouse=True)
def clean_state(broker):
    """Чистый стенд перед каждым тестом.

    Чистится и база, и Mailpit, и очереди: тест на «ровно одно письмо»
    бессмысленен, если в приёмнике осталось письмо от предыдущего.
    """
    _wipe_database()
    Mailpit.clear()
    for queue in (
        topology.QUEUE_BUILD_EMAIL,
        topology.QUEUE_SEND_EMAIL,
        topology.QUEUE_FANOUT,
        topology.QUEUE_RETRY_BUILD,
        topology.QUEUE_RETRY_SEND,
        topology.QUEUE_DEAD_LETTERS,
        topology.QUEUE_REPORTS,
    ):
        Rabbit.purge(queue)
    yield
    _wipe_database()


def _wipe_database() -> None:
    InboxMessage.objects.all().delete()
    DeliveryAttempt.objects.all().delete()
    DeliveryTask.objects.all().delete()
    ScheduledRun.objects.all().delete()
    OutboxMessage.objects.all().delete()
    CampaignSchedule.objects.all().delete()
    # Привязки — ДО рассылок: у них PROTECT на рассылку, и обратный порядок
    # завалил бы уборку в каждом тесте.
    EventBinding.objects.all().delete()
    Campaign.objects.all().delete()
    MessageTemplate.objects.all().delete()
    SegmentMember.objects.all().delete()
    Segment.objects.all().delete()
    ChannelOptout.objects.all().delete()
    Subscriber.objects.all().delete()


@pytest.fixture
def template() -> MessageTemplate:
    return MessageTemplate.objects.create(
        code='functional',
        name='Функциональный шаблон',
        channel=Channel.EMAIL.value,
        subject_template='Привет, {{ display_name }}',
        body_template='<p>Привет, {{ full_name }}! Смотрите «{{ film_title }}».</p>',
        allowed_variables=[
            'login',
            'email',
            'first_name',
            'last_name',
            'full_name',
            'display_name',
            'film_title',
            'campaign_title',
            'unsubscribe_url',
        ],
        sample_context={'login': 'ivan', 'display_name': 'Иван', 'full_name': 'Иван Петров', 'film_title': 'Дюна'},
    )


def register_user(
    login: str,
    email: str,
    *,
    first_name: str = 'Иван',
    last_name: str = 'Петров',
    password: str = 'Str0ng-Pass!23',
) -> dict[str, Any]:
    """Завести пользователя в Auth и вернуть его так, как отдал сам Auth.

    База Auth, в отличие от базы нотификаций, между тестами НЕ чистится: она
    общая и на неё смотрит контейнер воркера. Поэтому повторная регистрация того
    же логина — штатный случай, и обрабатывается она приведением имени к
    ожидаемому: иначе тест, меняющий имя через профиль, отравлял бы следующий.

    X-Request-Id обязателен: RequestIdMiddleware в Auth отвечает 400 на запрос
    без него ещё до обработчика.
    """
    response = requests.post(
        f'{AUTH_URL}/api/v1/auth/register',
        json={
            'login': login,
            'email': email,
            'password': password,
            'first_name': first_name,
            'last_name': last_name,
        },
        headers={'X-Request-Id': f'functional-{login}'},
        timeout=10,
    )
    if response.status_code in (200, 201):
        return response.json()

    assert response.status_code == 400, response.text
    return set_user_names(auth_login(login, password), first_name=first_name, last_name=last_name)


def set_user_names(token: str, *, first_name: str, last_name: str) -> dict[str, Any]:
    """Сменить имя и фамилию в Auth — источнике этих данных для рассылки."""
    response = requests.patch(
        f'{AUTH_URL}/api/v1/users/me/profile',
        json={'first_name': first_name, 'last_name': last_name},
        headers={'Authorization': f'Bearer {token}', 'X-Request-Id': 'functional-profile'},
        timeout=10,
    )
    assert response.status_code == 200, response.text
    return response.json()


def auth_login(login: str, password: str = 'Str0ng-Pass!23') -> str:
    response = requests.post(
        f'{AUTH_URL}/api/v1/auth/login',
        json={'login': login, 'password': password},
        headers={'X-Request-Id': f'functional-login-{login}'},
        timeout=10,
    )
    assert response.status_code == 200, response.text
    return response.json()['access_token']


def make_subscriber(login: str, email: str, **kwargs) -> Subscriber:
    """Пользователь в Auth плюс строка в витрине с ТЕМ ЖЕ идентификатором.

    Совпадение id — не деталь набора: на нём держится весь резолв. Подписчик,
    существующий только локально, для формирующего воркера — `unknown_user`.
    """
    user = register_user(login, email, **kwargs)
    return Subscriber.objects.create(id=user['id'], login=login, email=email, timezone='Europe/Moscow')


@pytest.fixture
def subscriber() -> Subscriber:
    return make_subscriber('ivan', 'ivan@example.com')


@pytest.fixture
def campaign(template) -> Campaign:
    return Campaign.objects.create(
        name='Функциональная рассылка',
        channel=Channel.EMAIL.value,
        template=template,
        context={'film_title': 'Дюна'},
        # Тихие часы на стенде включены и накрывают сутки целиком (см.
        # docker-compose.test.yml): иначе они не проверялись бы вообще. Общая
        # рассылка от них отписана — все прочие тесты про то, что письмо
        # доходит, а не про то, когда именно. Тест тихих часов заводит свою.
        respect_quiet_hours=False,
    )


@pytest.fixture
def welcome_binding(campaign) -> EventBinding:
    """Привязка события регистрации к рассылке — с созданием подписчика.

    Регистрация мгновенна, а витрина подписчиков синхронизируется периодически:
    человека в ней ещё нет, и контакт приносит само событие.
    """
    return EventBinding.objects.create(
        event_type=DomainEvent.USER_REGISTERED.value,
        campaign=campaign,
        audience=EventAudience.SUBJECT.value,
        upsert_subscriber=True,
    )


@pytest.fixture
def film_binding(campaign) -> EventBinding:
    return EventBinding.objects.create(
        event_type=DomainEvent.FILM_PUBLISHED.value,
        campaign=campaign,
        audience=EventAudience.CAMPAIGN.value,
    )


@pytest.fixture
def direct_binding(campaign) -> EventBinding:
    return EventBinding.objects.create(
        event_type=DomainEvent.NOTIFICATION_DIRECT.value,
        campaign=campaign,
        audience=EventAudience.SUBJECT.value,
    )


def post_event(payload: dict, *, token: str | None = None) -> requests.Response:
    """Отправить заявку в приём событий так же, как это делает чужой сервис."""
    return requests.post(
        f'{NOTIFICATIONS_URL}/api/v1/notifications/events',
        json=payload,
        headers={'X-Internal-Token': app_settings.NOTIFY_INTAKE_TOKEN if token is None else token},
        timeout=10,
    )


def post_message(payload: dict, *, token: str | None = None) -> requests.Response:
    return requests.post(
        f'{NOTIFICATIONS_URL}/api/v1/notifications/messages',
        json=payload,
        headers={'X-Internal-Token': app_settings.NOTIFY_INTAKE_TOKEN if token is None else token},
        timeout=10,
    )


def access_token(
    subject: str,
    *,
    jti: str = 'test-jti',
    token_type: str = 'access',
    secret: str | None = None,
    **claims,
) -> str:
    """Подписать токен тем же общим секретом, которым его подписывает Auth.

    Контейнера Auth в этом стенде нет, и в этом весь смысл: проверка подписи
    локальная, сетевого вызова на пути пользовательского запроса не существует.
    """
    payload = {
        'sub': subject,
        'jti': jti,
        'type': token_type,
        'roles': ['user'],
        'exp': datetime.now(UTC) + timedelta(minutes=15),
        **claims,
    }
    return jwt.encode(
        payload,
        secret or app_settings.AUTHJWT_SECRET_KEY,
        algorithm=app_settings.NOTIFY_JWT_ALGORITHM,
    )


def cabinet_post(path: str, *, token: str | None = None) -> requests.Response:
    headers = {'Authorization': f'Bearer {token}'} if token else {}
    return requests.post(f'{NOTIFICATIONS_URL}{path}', headers=headers, timeout=10)


def cabinet_get(path: str, *, token: str | None = None, **params) -> requests.Response:
    headers = {'Authorization': f'Bearer {token}'} if token else {}
    return requests.get(f'{NOTIFICATIONS_URL}{path}', headers=headers, params=params, timeout=10)


@pytest.fixture
def auth_redis():
    """Клиент денилиста — того самого Redis, который ведёт Auth."""
    client = redis.Redis(
        host=app_settings.AUTH_REDIS_HOST,
        port=app_settings.AUTH_REDIS_PORT,
        db=app_settings.AUTH_REDIS_DB,
        decode_responses=True,
    )
    yield client
    client.close()


def publish_raw(channel, *, routing_key: str, body: bytes, headers: dict | None = None) -> None:
    """Опубликовать произвольное тело — для проверок разбора и dead-letters."""
    channel.basic_publish(
        exchange=topology.EXCHANGE_EVENTS,
        routing_key=routing_key,
        body=body,
        properties=pika.BasicProperties(delivery_mode=2, headers=headers or {}),
    )


def publish_json(channel, *, exchange: str, routing_key: str, payload: dict, headers: dict | None = None) -> None:
    channel.basic_publish(
        exchange=exchange,
        routing_key=routing_key,
        body=json.dumps(payload).encode('utf-8'),
        properties=pika.BasicProperties(delivery_mode=2, headers=headers or {}),
    )


def wait_until(predicate, *, timeout: float = DELIVERY_TIMEOUT, message: str = 'условие не выполнилось'):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.3)
    raise AssertionError(f'{message} за {timeout} с')
