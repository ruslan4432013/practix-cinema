"""Одноразовые ticket'ы: выдача, гашение и отказ при недоступном хранилище."""

import json

import pytest

from practix_notifications_ws.core.config import settings
from practix_notifications_ws.services import tickets
from practix_notifications_ws.services.tickets import TicketError, TicketPayload

PAYLOAD = TicketPayload(subject='user-1', jti='jti-1', expires_at=4_102_444_800)


async def test_issue_returns_a_random_ticket(redis):
    first = await tickets.issue(redis, PAYLOAD)
    second = await tickets.issue(redis, PAYLOAD)

    assert first != second
    assert len(first) > 20, 'короткий ticket подбирался бы за время своей же жизни'


async def test_ticket_carries_identity_but_not_the_token(redis):
    """В хранилище лежит ровно то, чем управляют соединением, — и ничего сверх.

    Самого JWT там нет: ticket не должен быть способом ЕГО достать.
    """
    ticket = await tickets.issue(redis, PAYLOAD)
    stored = json.loads(redis.data[f'{settings.NOTIFY_WS_TICKET_PREFIX}{ticket}'])

    assert stored == {'sub': 'user-1', 'jti': 'jti-1', 'exp': 4_102_444_800}


async def test_redeem_returns_identity(redis):
    ticket = await tickets.issue(redis, PAYLOAD)

    assert await tickets.redeem(redis, ticket) == PAYLOAD


async def test_ticket_is_single_use(redis):
    """Ради этого он и одноразовый: перехваченный из access-лога ticket уже
    погашен тем, кто открыл соединение первым."""
    ticket = await tickets.issue(redis, PAYLOAD)
    await tickets.redeem(redis, ticket)

    with pytest.raises(TicketError):
        await tickets.redeem(redis, ticket)


async def test_unknown_ticket_is_rejected(redis):
    with pytest.raises(TicketError):
        await tickets.redeem(redis, 'made-up')


async def test_empty_ticket_is_rejected(redis):
    """Подключение без ticket'а вообще не должно доходить до похода в Redis."""
    with pytest.raises(TicketError):
        await tickets.redeem(redis, '')


async def test_storage_failure_closes_the_door(redis):
    """Недоступный Redis — это отказ, а не «пускаем без проверки».

    Та же политика, что у денилиста: неизвестно — значит нет.
    """
    redis.explode = ConnectionError('redis is down')
    with pytest.raises(TicketError):
        await tickets.redeem(redis, 'anything')


async def test_malformed_payload_is_rejected(redis):
    redis.data[f'{settings.NOTIFY_WS_TICKET_PREFIX}broken'] = 'not json'
    with pytest.raises(TicketError):
        await tickets.redeem(redis, 'broken')
