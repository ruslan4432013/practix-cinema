"""Уборка протухших ссылок пачками.

Здесь проверяется не только результат, но и то, что запрос уборщика
(`DELETE ... WHERE code IN (SELECT ... LIMIT :n FOR UPDATE SKIP LOCKED)`)
принимается настоящим PostgreSQL: заглушка из юнит-набора об этом ничего сказать
не может.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from practix_link_shortener.services.link_service import LinkService

_INSERT = text("""
    INSERT INTO short_link (code, kind, target_url, expires_at)
    VALUES (:code, 'redirect', 'http://localhost/', :expires_at)
""")


async def insert_link(db, code: str, expires_at: datetime) -> None:
    await db.execute(_INSERT, {'code': code, 'expires_at': expires_at})


async def codes(db) -> list[str]:
    result = await db.execute(text('SELECT code FROM short_link ORDER BY code'))
    return [row[0] for row in result]


async def test_purge_removes_expired_in_batches_and_spares_the_live_one(db):
    now = datetime.now(UTC)
    for i in range(5):
        await insert_link(db, f'dead{i:03}', now - timedelta(days=40 + i))
    await insert_link(db, 'alive01', now + timedelta(hours=1))
    await db.commit()

    # Пачка меньше числа протухших строк: цикл обязан сделать несколько заходов,
    # а не остановиться на первом.
    removed = await LinkService(db).purge_expired(before=now - timedelta(days=30), batch_size=2)

    assert removed == 5
    assert await codes(db) == ['alive01']


async def test_purge_does_not_touch_links_expired_after_the_cutoff(db):
    """Отсрочка `--before-days` — это ответ на «почему у человека 404»."""
    now = datetime.now(UTC)
    await insert_link(db, 'recent1', now - timedelta(hours=1))
    await insert_link(db, 'ancient', now - timedelta(days=40))
    await db.commit()

    removed = await LinkService(db).purge_expired(before=now - timedelta(days=30))

    assert removed == 1
    assert await codes(db) == ['recent1']


async def test_purge_on_empty_table_returns_zero(db):
    assert await LinkService(db).purge_expired(before=datetime.now(UTC)) == 0
