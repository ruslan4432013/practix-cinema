"""Резолв ссылки и различение двух причин IntegrityError.

Асинхронные функции здесь гоняются через ``asyncio.run`` вручную: набор
синхронный по построению (см. pytest.ini), а сессия подменена заглушкой — база
для этих правил не нужна.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError

from practix_link_shortener.models.entity import KIND_CONFIRM_EMAIL, KIND_REDIRECT, ShortLink
from practix_link_shortener.services.link_service import (
    IDEMPOTENCY_CONSTRAINT,
    PK_CONSTRAINT,
    LinkService,
    _constraint_name,
    needs_confirmation,
)


class StubSession:
    """Минимальная сессия: умеет только отдать заранее положенную строку по PK."""

    def __init__(self, link: ShortLink | None):
        self._link = link

    async def get(self, _model, _pk):
        return self._link


def link(**overrides) -> ShortLink:
    defaults = {
        'code': 'AbC1234',
        'kind': KIND_REDIRECT,
        'user_id': None,
        'target_url': 'http://localhost/',
        'idempotency_key': None,
        'expires_at': datetime.now(UTC) + timedelta(hours=1),
        'revoked_at': None,
        'visit_count': 0,
    }
    return ShortLink(**{**defaults, **overrides})


def resolve(stored: ShortLink | None) -> ShortLink | None:
    return asyncio.run(LinkService(StubSession(stored)).resolve('AbC1234'))


def test_live_link_resolves():
    assert resolve(link()) is not None


def test_missing_link_resolves_to_none():
    assert resolve(None) is None


def test_expired_link_resolves_to_none():
    assert resolve(link(expires_at=datetime.now(UTC) - timedelta(seconds=1))) is None


def test_expiry_boundary_is_exclusive():
    """``expires_at == now()`` считается ИСТЁКШИМ: срок «до» не включает границу."""
    assert resolve(link(expires_at=datetime.now(UTC))) is None


def test_revoked_link_resolves_to_none():
    assert resolve(link(revoked_at=datetime.now(UTC))) is None


def test_confirmation_is_driven_by_kind_not_by_route():
    """Поведение выбирает вид ссылки. Второй маршрут над той же таблицей был бы обходом."""
    assert needs_confirmation(link(kind=KIND_CONFIRM_EMAIL, user_id=uuid.uuid4())) is True
    assert needs_confirmation(link(kind=KIND_REDIRECT)) is False


def integrity_error(constraint: str | None) -> IntegrityError:
    orig = Exception('duplicate key')
    if constraint is not None:
        orig.constraint_name = constraint
    return IntegrityError('INSERT ...', {}, orig)


def test_constraint_name_distinguishes_collision_from_idempotency():
    """Одна и та же вставка нарушает РАЗНЫЕ ограничения, и лечатся они по-разному.

    Коллизию кода надо перегенерировать; повтор ключа идемпотентности — вернуть
    существующую строку. Слепой повтор на любом IntegrityError крутился бы вечно
    на дубле ключа.
    """
    assert _constraint_name(integrity_error(PK_CONSTRAINT)) == PK_CONSTRAINT
    assert _constraint_name(integrity_error(IDEMPOTENCY_CONSTRAINT)) == IDEMPOTENCY_CONSTRAINT


def test_constraint_name_is_none_when_driver_does_not_report_it():
    """Незнакомое ограничение не должно молча попасть в ветку повтора."""
    assert _constraint_name(integrity_error(None)) is None
