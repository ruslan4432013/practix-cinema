"""Проверка доступности хранилища: три исхода и ни одного исключения наружу."""

import asyncio

import pytest

from practix_core.health import reachable


class Client:
    def __init__(self, *, error: Exception | None = None, delay: float = 0.0):
        self.error = error
        self.delay = delay
        self.calls = 0

    async def ping(self) -> str:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return 'PONG'


@pytest.mark.asyncio
async def test_live_client_is_reachable():
    assert await reachable(Client(), timeout=1.0) is True


@pytest.mark.asyncio
async def test_absent_client_is_the_same_as_a_broken_one():
    """Клиента не создали и клиент не отвечает — для вызывающего одно состояние."""
    assert await reachable(None, timeout=1.0) is False


@pytest.mark.asyncio
async def test_any_driver_error_reads_as_unreachable():
    assert await reachable(Client(error=ConnectionError('нет соединения')), timeout=1.0) is False
    assert await reachable(Client(error=RuntimeError('что угодно')), timeout=1.0) is False


@pytest.mark.asyncio
async def test_hanging_client_is_bounded_by_our_own_timeout():
    """«Чёрная дыра» вместо хранилища: пакеты дропаются, RST не приходит.

    Без собственного лимита проба висела бы столько, сколько отпущено сокету,
    умноженное на число повторов драйвера.
    """
    client = Client(delay=1.0)

    assert await reachable(client, timeout=0.01) is False


@pytest.mark.asyncio
async def test_ping_is_called_exactly_once():
    client = Client()

    await reachable(client, timeout=1.0)

    assert client.calls == 1
