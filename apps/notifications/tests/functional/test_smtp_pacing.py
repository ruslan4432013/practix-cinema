"""Темп отправки против НАСТОЯЩЕГО Redis.

Юнит-набор проверяет решения пейсера на заглушке; здесь проверяется то, чего
заглушкой не проверить, — что Lua-скрипт действительно выполняется в Redis и что
два независимых отправителя делят один слот. Именно это и было сломано: пауза
отсчитывалась от поля экземпляра, поэтому каждая реплика отправляющего воркера
добавляла в почтовый сервер ещё один полный лимит.
"""

import time

import pytest

from practix_notifications.channels.pacing import RatePacer
from practix_notifications.core.config import RATE_SCOPE_GLOBAL, RATE_SCOPE_PROCESS, settings

#: Медленный темп на время теста: при боевых 200 письмах в секунду разница между
#: общим и попроцессным лимитом уместилась бы в шум планировщика ОС.
RATE = 20.0
INTERVAL = 1.0 / RATE
SLOTS = 10
KEY = 'notifications:pace:test:smtp'


@pytest.fixture
def slot_key(auth_redis):
    auth_redis.delete(KEY)
    yield KEY
    auth_redis.delete(KEY)


@pytest.fixture
def slow_rate(monkeypatch):
    monkeypatch.setattr(settings, 'NOTIFY_SMTP_RATE_PER_SECOND', RATE)
    monkeypatch.setattr(settings, 'NOTIFY_SMTP_RATE_MAX_WAIT', 30.0)


def drain(pacers, slots: int) -> float:
    """Занять ``slots`` слотов по очереди всеми «репликами». Вернуть время."""
    started = time.monotonic()
    for index in range(slots):
        pacers[index % len(pacers)].wait_for_slot()
    return time.monotonic() - started


def test_two_replicas_share_one_pace(slot_key, slow_rate):
    """Два процесса на один ключ дают ОДИН темп, а не два.

    Десять слотов при 20 в секунду — это не меньше девяти интервалов ожидания
    независимо от того, сколько отправителей их разбирают.
    """
    replicas = [RatePacer(key=slot_key), RatePacer(key=slot_key)]

    elapsed = drain(replicas, SLOTS)

    assert elapsed >= (SLOTS - 1) * INTERVAL * 0.9


def test_per_process_pace_is_what_the_shared_slot_fixes(slot_key, slow_rate, monkeypatch):
    """Контрольный замер: без общего слота те же две «реплики» идут вдвое быстрее.

    Это не проверка режима ``process`` ради него самого — это цифра, ради
    которой общий слот и появился.
    """
    monkeypatch.setattr(settings, 'NOTIFY_SMTP_RATE_SCOPE', RATE_SCOPE_PROCESS)
    replicas = [RatePacer(key=slot_key), RatePacer(key=slot_key)]

    elapsed = drain(replicas, SLOTS)

    # Каждая реплика выдержала свои интервалы, но они шли параллельно.
    assert elapsed < (SLOTS - 1) * INTERVAL * 0.75


def test_slot_key_is_volatile(slot_key, slow_rate, auth_redis):
    """У ключа обязан быть срок жизни: Redis ядра работает в volatile-lru, и
    вечный ключ там невытесняем."""
    assert settings.NOTIFY_SMTP_RATE_SCOPE == RATE_SCOPE_GLOBAL

    RatePacer(key=slot_key).wait_for_slot()

    assert auth_redis.pttl(slot_key) > 0
