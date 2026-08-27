"""Темп отправки: общий на сервис, а не на процесс.

Главный тест здесь — ``test_two_pacers_share_one_slot``: именно он падал бы на
прежней реализации, где пауза отсчитывалась от поля экземпляра и при N репликах
воркера в почтовый сервер уходило N лимитов.
"""

import time

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from practix_notifications.channels.base import TemporaryDeliveryError
from practix_notifications.channels.pacing import DEGRADED_SECONDS, RatePacer
from practix_notifications.core.config import RATE_SCOPE_GLOBAL, RATE_SCOPE_PROCESS, settings

KEY = 'notifications:pace:email:mailpit'


class FakeRedis:
    """Учёт вызовов скрипта вместо настоящего Redis."""

    def __init__(self, wait_us: int = 0, explode: Exception | None = None) -> None:
        self.wait_us = wait_us
        self.explode = explode
        self.calls: list[dict] = []

    def register_script(self, script: str):
        def run(keys, args):
            if self.explode is not None:
                raise self.explode
            self.calls.append({'key': keys[0], 'args': args})
            return self.wait_us

        return run


@pytest.fixture
def global_scope(monkeypatch):
    monkeypatch.setattr(settings, 'NOTIFY_SMTP_RATE_SCOPE', RATE_SCOPE_GLOBAL)


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    """Паузы вместо ожидания: тест проверяет решение, а не терпение раннера."""
    recorded: list[float] = []
    monkeypatch.setattr(time, 'sleep', recorded.append)
    return recorded


def pacer(client: FakeRedis, key: str = KEY) -> RatePacer:
    return RatePacer(key=key, client_factory=lambda: client)


def test_two_pacers_share_one_slot(global_scope, sleeps):
    """Суть исправления: две «реплики» считают темп в ОДНОМ месте.

    Раньше состояние жило в поле экземпляра, поэтому второй процесс отправлял
    со своей полной скоростью, и суммарный поток был кратен числу реплик.
    """
    redis = FakeRedis()
    pacer(redis).wait_for_slot()
    pacer(redis).wait_for_slot()

    assert [call['key'] for call in redis.calls] == [KEY, KEY]


def test_pacer_waits_exactly_what_redis_returned(global_scope, sleeps):
    pacer(FakeRedis(wait_us=250_000)).wait_for_slot()

    assert sleeps == [pytest.approx(0.25)]


def test_interval_comes_from_the_configured_rate(global_scope, sleeps, monkeypatch):
    monkeypatch.setattr(settings, 'NOTIFY_SMTP_RATE_PER_SECOND', 25.0)
    monkeypatch.setattr(settings, 'NOTIFY_SMTP_RATE_MAX_WAIT', 2.0)
    redis = FakeRedis()

    pacer(redis).wait_for_slot()

    interval_us, max_wait_us, _ttl_ms = redis.calls[0]['args']
    assert interval_us == 40_000
    assert max_wait_us == 2_000_000


def test_refused_slot_becomes_a_temporary_failure(global_scope, sleeps):
    """Ждать дольше потолка нельзя: получатель уезжает в штатный ярус повторов.

    Иначе воркер встал бы внутри пачки на неопределённое время — а пока он там
    стоит, pika не обслуживает соединение.
    """
    with pytest.raises(TemporaryDeliveryError):
        pacer(FakeRedis(wait_us=-1)).wait_for_slot()

    assert sleeps == []


def test_unavailable_redis_degrades_to_the_per_process_pace(global_scope, sleeps):
    """Отказ хранилища ручки — не отказ доставки.

    Письмо уходит, лимит становится попроцессным (то есть прежним), и в Redis
    больше не стучатся — иначе недоступность стоила бы таймаута на КАЖДОЕ письмо.
    """
    redis = FakeRedis(explode=RedisConnectionError('no route to host'))
    subject = pacer(redis)

    subject.wait_for_slot()
    redis.explode = None
    subject.wait_for_slot()

    # Предохранитель ещё не истёк, поэтому второй вызов Redis не трогает.
    assert redis.calls == []
    # Первая пауза не нужна (отправок ещё не было), вторая — попроцессный шаг.
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(1.0 / settings.NOTIFY_SMTP_RATE_PER_SECOND, abs=0.005)


def test_degraded_pacer_returns_to_redis_after_the_fuse(global_scope, sleeps, monkeypatch):
    redis = FakeRedis(explode=RedisConnectionError('no route to host'))
    subject = pacer(redis)
    subject.wait_for_slot()

    redis.explode = None
    later = time.monotonic() + DEGRADED_SECONDS + 1
    monkeypatch.setattr(time, 'monotonic', lambda: later)
    subject.wait_for_slot()

    assert len(redis.calls) == 1


def test_process_scope_never_touches_redis(monkeypatch, sleeps):
    """Аварийный выключатель: режим `process` обязан работать без Redis вовсе."""
    monkeypatch.setattr(settings, 'NOTIFY_SMTP_RATE_SCOPE', RATE_SCOPE_PROCESS)
    redis = FakeRedis()
    subject = pacer(redis)

    subject.wait_for_slot()
    subject.wait_for_slot()

    assert redis.calls == []
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(1.0 / settings.NOTIFY_SMTP_RATE_PER_SECOND, abs=0.005)
