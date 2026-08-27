"""Бюджет повторов отправки: отсрочка и отказ ждут разного и стоят разного.

Оба исхода возвращают получателя в парковочную очередь, но по совершенно разным
причинам, и раньше они были склеены в один список. Последствий было два, и оба
тихие.

**Отказ не заканчивался никогда.** Парковка идёт по ветке ``ACK``, поэтому общая
проверка бюджета в ``broker.consumer`` до неё не доходит вовсе, а
``DeliveryTask.attempts`` ни с чем не сравнивался. Получатель, чья ошибка
называется временной, а является постоянной, ходил по кругу
``send-email → retry-send-email`` бесконечно и в dead-letters не попадал ни разу
— то есть ``NOTIFY_MAX_ATTEMPTS`` на очереди отправки не действовал.

**Отсрочка расходовала бюджет, которого не должна была касаться.** Счётчик в
заголовке рос на каждом отскоке, а ночное окно в восемь часов при TTL парковки в
тридцать секунд — это около тысячи отскоков. Пачка доживала до утра с
``x-attempt`` под тысячу, и первый же настоящий ``RETRY`` отправлял её в
dead-letters вместо повтора.
"""

import uuid
from datetime import UTC, datetime

import pytest

from practix_notifications.broker import topology
from practix_notifications.broker.consumer import Outcome
from practix_notifications.broker.envelope import PreparedMessage, build_notification_prepared
from practix_notifications.campaigns.models import DeliveryTask, ScheduledRun
from practix_notifications.channels import RenderedMessage, TemporaryDeliveryError
from practix_notifications.core.config import settings
from practix_notifications.enums import AttemptResult, RunStatus, TaskStatus
from practix_notifications.services.delivery import handle_notification_prepared
from practix_notifications.subscribers.models import Subscriber

pytestmark = pytest.mark.django_db


class FakeBroker:
    """Брокер, запоминающий публикации вместе с номером попытки."""

    def __init__(self) -> None:
        self.published: list[dict] = []
        self.parked: list[dict] = []

    def publish(self, **kwargs) -> None:
        self.published.append(kwargs)

    def republish_to_retry(self, *, body: dict, headers: dict, attempt: int, routing_key: str) -> None:
        self.parked.append({'body': body, 'attempt': attempt, 'routing_key': routing_key})

    def process_events(self, seconds: float = 0) -> None:
        pass


class FailingPool:
    """Пул отправителей, у которого почтовый сервер всегда отвечает отказом."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def get(self, channel: str):
        return self

    def send(self, address: str, message: RenderedMessage) -> None:
        raise self.exc


class SilentPool:
    """Отправитель, который никогда не вызывается: до него не доходит дело."""

    def get(self, channel: str):
        return self

    def send(self, address: str, message: RenderedMessage) -> None:  # pragma: no cover — не должно вызываться
        raise AssertionError('до отправки дело дойти не должно')


@pytest.fixture
def run(campaign) -> ScheduledRun:
    return ScheduledRun.objects.create(
        campaign=campaign,
        run_key=str(uuid.uuid4()),
        planned_for=datetime.now(UTC),
        subject_snapshot='Тема',
        body_snapshot='Тело',
        status=RunStatus.PUBLISHED.value,
    )


def _task(run: ScheduledRun, subscriber: Subscriber, *, attempts: int = 0) -> DeliveryTask:
    return DeliveryTask.objects.create(
        run=run,
        campaign=run.campaign,
        subscriber=subscriber,
        channel='email',
        address=subscriber.email,
        idempotency_key=str(uuid.uuid4()),
        status=TaskStatus.QUEUED.value,
        attempts=attempts,
    )


def _body(run: ScheduledRun, tasks: list[DeliveryTask]) -> dict:
    return build_notification_prepared(
        run_id=str(run.id),
        campaign_id=str(run.campaign_id),
        channel='email',
        category='marketing',
        content_id='',
        recipients=[
            PreparedMessage(
                task_id=str(task.id),
                idempotency_key=task.idempotency_key,
                subscriber_id=str(task.subscriber_id),
                address=task.address,
                subject='Тема',
                body='Тело',
            )
            for task in tasks
        ],
        valid_until=None,
    )


class TestTemporaryFailureBudget:
    @pytest.fixture(autouse=True)
    def _daytime(self, monkeypatch):
        """Тихие часы выключены: здесь проверяется отказ, а не отсрочка.

        Без этого набор зависел бы от времени суток запуска — окно по умолчанию
        22:00–09:00 в Москве, и вечерний прогон отправлял бы всех в отсрочку.
        """
        monkeypatch.setattr(settings, 'NOTIFY_QUIET_HOURS_ENABLED', False)

    def test_temporary_failure_is_parked_while_the_budget_lasts(self, run, subscriber):
        task = _task(run, subscriber)
        broker = FakeBroker()

        outcome = handle_notification_prepared(
            broker, FailingPool(TemporaryDeliveryError('почтовый сервер молчит')), _body(run, [task]), attempt=0
        )

        assert outcome is Outcome.ACK
        assert len(broker.parked) == 1
        task.refresh_from_db()
        assert task.status == TaskStatus.PENDING.value
        assert task.attempts == 1

    def test_temporary_failure_gives_up_once_the_budget_is_spent(self, run, subscriber):
        """Главная правка: «временный» отказ обязан когда-то стать окончательным.

        Без потолка эта задача возвращалась бы в очередь вечно — она никогда не
        доходила до dead-letters, потому что парковка идёт по ветке ACK.
        """
        task = _task(run, subscriber, attempts=settings.NOTIFY_MAX_ATTEMPTS - 1)
        broker = FakeBroker()

        outcome = handle_notification_prepared(
            broker, FailingPool(TemporaryDeliveryError('почтовый сервер молчит')), _body(run, [task]), attempt=0
        )

        assert outcome is Outcome.ACK
        assert broker.parked == [], 'исчерпавший бюджет получатель не должен возвращаться в очередь'
        task.refresh_from_db()
        assert task.status == TaskStatus.FAILED.value
        assert 'attempts exhausted' in task.last_error
        assert task.delivery_attempts.filter(result=AttemptResult.FAILED.value).exists()

    def test_failed_recipient_counts_towards_the_campaign(self, run, subscriber):
        task = _task(run, subscriber, attempts=settings.NOTIFY_MAX_ATTEMPTS)
        broker = FakeBroker()

        handle_notification_prepared(
            broker, FailingPool(TemporaryDeliveryError('отказ')), _body(run, [task]), attempt=0
        )

        run.campaign.refresh_from_db()
        assert run.campaign.failed_count == 1


class TestQuietHoursDeferral:
    @pytest.fixture(autouse=True)
    def _quiet_now(self, monkeypatch):
        """Тихие часы вокруг любого момента: окно 00:00–23:59 накрывает всё."""
        monkeypatch.setattr(settings, 'NOTIFY_QUIET_HOURS_ENABLED', True)
        monkeypatch.setattr(settings, 'NOTIFY_QUIET_HOURS_START', '00:00')
        monkeypatch.setattr(settings, 'NOTIFY_QUIET_HOURS_END', '23:59')

    def test_deferral_does_not_spend_an_attempt(self, run, subscriber):
        """Номер попытки у отложенного возвращается ТЕМ ЖЕ.

        Иначе ночь длиной в восемь часов при тридцатисекундном TTL парковки
        накрутила бы около тысячи, и утром пачка ушла бы в dead-letters с первой
        же настоящей ошибкой.
        """
        task = _task(run, subscriber)
        broker = FakeBroker()

        outcome = handle_notification_prepared(broker, SilentPool(), _body(run, [task]), attempt=7)

        assert outcome is Outcome.ACK
        assert len(broker.parked) == 1
        assert broker.parked[0]['attempt'] == 7
        assert broker.parked[0]['routing_key'] == topology.RK_NOTIFICATION_PREPARED
        task.refresh_from_db()
        assert task.status == TaskStatus.PENDING.value
        assert task.attempts == 0, 'отсрочка — не попытка отправки'

    def test_deferral_survives_a_thousand_bounces(self, run, subscriber):
        """Модель ночи целиком: отскоки не накапливаются ни в каком счётчике."""
        task = _task(run, subscriber)
        broker = FakeBroker()
        attempt = 0

        for _ in range(50):
            handle_notification_prepared(broker, SilentPool(), _body(run, [task]), attempt=attempt)
            attempt = broker.parked[-1]['attempt']

        assert attempt == 0
        task.refresh_from_db()
        assert task.attempts == 0


def test_deferred_and_failed_travel_in_separate_messages(run, subscriber, monkeypatch):
    """Смешанная пачка: одному ночь, другому отказ — и уезжают они порознь.

    Общая публикация означала бы, что либо отказ получает бесплатный повтор,
    либо отсрочка тратит бюджет. Оба варианта — это исходная ошибка.
    """
    quiet = Subscriber.objects.create(id=uuid.uuid4(), login='night', email='night@example.com', timezone='UTC')
    # Ночь у одного и день у другого: окно считается по таймзоне ПОЛУЧАТЕЛЯ,
    # поэтому достаточно развести их по поясам и подменить сам предикат.
    monkeypatch.setattr('practix_notifications.services.delivery._is_quiet', lambda r, tz, now: tz == 'UTC')
    tasks = [_task(run, quiet), _task(run, subscriber)]
    broker = FakeBroker()

    handle_notification_prepared(broker, FailingPool(TemporaryDeliveryError('отказ')), _body(run, tasks), attempt=3)

    attempts = sorted(item['attempt'] for item in broker.parked)
    assert attempts == [3, 4], 'отложенный уезжает с прежним номером, отказавший — со следующим'
