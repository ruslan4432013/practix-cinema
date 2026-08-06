"""Рукопожатие websocket: подписка снимается с учёта при любом исходе.

Единственный набор, который трогает саму ``stream()``, и появился он из-за
утечки, жившей ровно в незакрытом ею куске.

``hub.subscribe`` увеличивает счётчики соединений, а снимает их ``finally``.
Между этими двумя точками стояли ``accept()`` и отправка кадра ``hello`` — и обе
выполнялись ВНЕ ``try``. Клиент, ушедший между проверками и рукопожатием
(агрессивный цикл переподключения делает это регулярно), уносил исключение мимо
``finally``: подписка оставалась в реестре навсегда. Утечка тихая и
накопительная — процесс постепенно начинал отвечать ``4429`` всем подряд, и
чинил это только перезапуск.

Настоящий транспорт здесь не нужен: проверяется бухгалтерия реестра, а не
протокол. Поэтому ``WebSocket`` подменён двойником, который умеет падать там,
где падал бы настоящий.
"""

import json

import pytest

from practix_notifications_ws.api.v1 import stream as stream_module
from practix_notifications_ws.services import guard
from practix_notifications_ws.services.hub import ConnectionHub

USER = 'user-a'
TICKET = 'ticket-value'


class FakeWebSocket:
    """Сокет, который умеет отвалиться на заданном шаге рукопожатия."""

    def __init__(self, *, origin: str | None = None, fail_on: str = '') -> None:
        self.headers = {'origin': origin} if origin is not None else {}
        self.fail_on = fail_on
        self.accepted = False
        self.closed_with: int | None = None
        self.sent: list[dict] = []

    async def accept(self) -> None:
        if self.fail_on == 'accept':
            raise ConnectionResetError('клиент ушёл до рукопожатия')
        self.accepted = True

    async def send_json(self, payload: dict) -> None:
        if self.fail_on == 'hello':
            raise ConnectionResetError('клиент ушёл сразу после рукопожатия')
        self.sent.append(payload)

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code

    async def receive_text(self) -> str:  # pragma: no cover — до чтения дело не доходит
        raise AssertionError('в этих сценариях клиента уже нет')


@pytest.fixture
def wired(monkeypatch, redis):
    """Шлюз с настоящим реестром и подменённым внешним миром."""
    hub = ConnectionHub(queue_size=3, max_per_user=2, max_total=4)
    monkeypatch.setattr(stream_module, 'get_hub', lambda: hub)
    monkeypatch.setattr(stream_module.redis_db, 'get_client', _const(redis))
    monkeypatch.setattr(guard, 'origin_allowed', lambda origin: True)
    monkeypatch.setattr(guard, 'still_valid', _const(True))
    # Ticket кладётся в хранилище напрямую: выдача проверяется своим набором.
    redis.data[f'{stream_module.settings.NOTIFY_WS_TICKET_PREFIX}{TICKET}'] = json.dumps(
        {'sub': USER, 'jti': 'jti-1', 'exp': 0}
    )
    return hub


def _const(value):
    async def _call(*args, **kwargs):
        return value

    return _call


@pytest.mark.asyncio
@pytest.mark.parametrize('fail_on', ['accept', 'hello'])
async def test_subscription_is_released_when_the_client_vanishes(wired, fail_on):
    """Обрыв на рукопожатии не должен оставлять запись в реестре.

    Исключение по-прежнему уходит наверх — его обрабатывает Starlette; наше дело
    только в том, чтобы счётчики после него были чистыми.
    """
    websocket = FakeWebSocket(fail_on=fail_on)

    with pytest.raises(ConnectionResetError):
        await stream_module.stream(websocket, ticket=TICKET)

    assert wired.total == 0
    assert wired.count_for(USER) == 0


@pytest.mark.asyncio
async def test_repeated_broken_handshakes_do_not_exhaust_the_limit(wired, redis):
    """Тот же сценарий, повторённый до отказа: раньше он выбивал пользователя.

    Лимит на пользователя здесь — два. Пять сорванных рукопожатий подряд при
    утечке заняли бы обе ячейки навсегда, и все последующие попытки этого
    человека получали бы ``4429`` до перезапуска процесса.
    """
    key = f'{stream_module.settings.NOTIFY_WS_TICKET_PREFIX}{TICKET}'
    for _ in range(5):
        # Ticket одноразовый (GETDEL), поэтому на каждую попытку кладём новый.
        redis.data[key] = json.dumps({'sub': USER, 'jti': 'jti-1', 'exp': 0})
        with pytest.raises(ConnectionResetError):
            await stream_module.stream(FakeWebSocket(fail_on='accept'), ticket=TICKET)

    assert wired.count_for(USER) == 0
    assert wired.total == 0


@pytest.mark.asyncio
async def test_rejected_origin_never_reaches_the_registry(wired, monkeypatch):
    """Отказ по Origin — это close() ДО accept() и без записи в реестре."""
    monkeypatch.setattr(guard, 'origin_allowed', lambda origin: False)
    websocket = FakeWebSocket(origin='http://evil.example')

    await stream_module.stream(websocket, ticket=TICKET)

    assert websocket.accepted is False
    assert websocket.closed_with == guard.WS_CLOSE_FORBIDDEN
    assert wired.total == 0
