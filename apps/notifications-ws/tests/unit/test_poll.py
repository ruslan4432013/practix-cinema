"""Long polling — вторая ступень деградации.

Ручка вызывается напрямую, с поддельным ``AuthJWT``: поднимать приложение
целиком ради неё значило бы тянуть в юнит-набор Redis и RabbitMQ, а проверяется
здесь поведение самой ступени — сколько она ждёт, что отдаёт и что убирает за
собой.
"""

import asyncio

import pytest
from fastapi import HTTPException

from practix_notifications_ws.api.v1 import stream
from practix_notifications_ws.core.config import settings
from practix_notifications_ws.services import providers

FRAME = {'type': 'notification', 'data': {'task_id': 't1'}}


class FakeAuthorize:
    """``jwt_required`` уже отработал: здесь нужен только его результат."""

    def __init__(self, subject: str | None = 'user-1') -> None:
        self._subject = subject

    async def jwt_required(self) -> None:
        return None

    async def get_raw_jwt(self) -> dict:
        return {'sub': self._subject} if self._subject else {}


@pytest.fixture(autouse=True)
def _wire_hub(hub, monkeypatch):
    monkeypatch.setattr(providers, 'hub', hub)
    return hub


async def test_timeout_returns_an_empty_batch(hub):
    """Пустой ответ — нормальный исход, а не ошибка: клиент сразу переоткрывает."""
    response = await stream.poll(wait=1, authorize=FakeAuthorize())

    assert response['items'] == []
    assert response['transport'] == 'long-poll'


async def test_message_arriving_during_the_wait_is_returned(hub):
    async def publish_soon():
        await asyncio.sleep(0.05)
        hub.publish('user-1', FRAME)

    task = asyncio.create_task(publish_soon())
    response = await stream.poll(wait=5, authorize=FakeAuthorize())
    await task

    assert response['items'] == [FRAME]


async def test_whole_backlog_leaves_in_one_response(hub):
    """Открывать отдельный запрос на каждое сообщение одной пачки незачем."""

    async def publish_soon():
        await asyncio.sleep(0.05)
        for index in range(3):
            hub.publish('user-1', {'type': 'notification', 'data': {'task_id': f't{index}'}})

    task = asyncio.create_task(publish_soon())
    response = await stream.poll(wait=5, authorize=FakeAuthorize())
    await task

    assert len(response['items']) == 3


async def test_subscription_is_always_released(hub):
    """Утечка подписки на каждый опрос за час набрала бы их тысячи."""
    await stream.poll(wait=1, authorize=FakeAuthorize())

    assert hub.total == 0
    # Не только сокетный счётчик: теперь у поллеров свой потолок, и утёкшая
    # подписка выедала бы именно его — до отказа всем опросам пользователя.
    assert hub.pollers == 0


async def test_wait_is_clamped_to_the_ceiling(hub, monkeypatch):
    """Запрос длиннее таймаута промежуточного прокси вернулся бы клиенту
    обрывом вместо пустого ответа."""
    monkeypatch.setattr(settings, 'NOTIFY_WS_POLL_MAX_TIMEOUT', 1.0)

    response = await stream.poll(wait=600, authorize=FakeAuthorize())

    assert response['waited'] == 1.0


async def test_token_without_subject_is_rejected(hub):
    with pytest.raises(HTTPException) as exc:
        await stream.poll(wait=1, authorize=FakeAuthorize(subject=None))

    assert exc.value.status_code == 401


async def test_exhausted_poller_budget_is_a_429(hub):
    """Отказ, а не безлимит: каждый висящий запрос держит задачу и очередь
    кадров, и без потолка один пользователь исчерпал бы память процесса.

    ``Retry-After`` обязателен — иначе клиент вернётся мгновенно и будет
    молотить отказами; ступенью ниже у него в любом случае остаётся лента."""
    for _ in range(3):  # max_pollers_per_user фикстуры
        hub.subscribe('user-1', count_towards_limits=False)

    with pytest.raises(HTTPException) as exc:
        await stream.poll(wait=1, authorize=FakeAuthorize())

    assert exc.value.status_code == 429
    assert exc.value.headers['Retry-After'] == str(int(settings.NOTIFY_WS_POLL_TIMEOUT))
    # Отказ не оставил после себя подписку.
    assert hub.pollers == 3


async def test_a_rejected_poller_does_not_touch_the_socket_budget(hub):
    """Бюджеты раздельные: отказ деградировавшему клиенту не должен ни отнимать,
    ни занимать слот сокета."""
    for _ in range(3):
        hub.subscribe('user-1', count_towards_limits=False)

    with pytest.raises(HTTPException):
        await stream.poll(wait=1, authorize=FakeAuthorize())

    assert hub.total == 0
    assert hub.subscribe('user-1') is not None


async def test_dropped_frames_are_reported_first(hub):
    """Клиент должен узнать о дыре ДО того, как увидит следующее сообщение, —
    иначе решит, что ничего не пропустил."""

    async def flood():
        await asyncio.sleep(0.05)
        for index in range(5):  # очередь фикстуры на 3
            hub.publish('user-1', {'type': 'notification', 'data': {'task_id': f't{index}'}})

    task = asyncio.create_task(flood())
    response = await stream.poll(wait=5, authorize=FakeAuthorize())
    await task

    assert response['items'][0] == {'type': 'desync', 'dropped': 2}
