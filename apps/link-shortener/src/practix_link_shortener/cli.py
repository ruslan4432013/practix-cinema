"""Уборка протухших ссылок.

    python -m practix_link_shortener.cli purge --before-days 30

Зачем нужна. Ссылка живёт часы, а строка о ней — вечно: без уборки таблица
растёт линейно по числу регистраций и писем. Индекс ``short_link_expires_at_idx``
заведён ровно под этот запрос, а не под горячий путь (тот ходит по первичному
ключу).

Почему НЕ сразу после истечения. Строка протухшей ссылки — это ответ на вопрос
«почему у человека 404»: без неё разбор обращения упирается в «кода нет, и
никогда не было». Отсрочка ``--before-days`` даёт время на разбор.

Cron'а в стенде нет, поэтому команда ручная — как ``recount`` в UGC.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import typer

from practix_link_shortener.db.postgres import async_session, engine
from practix_link_shortener.services.link_service import LinkService

cli = typer.Typer(help='Обслуживание таблицы коротких ссылок.')


@cli.callback()
def _root() -> None:
    """Пустой callback: не даёт Typer схлопнуть приложение до одной команды."""


@cli.command()
def purge(
    before_days: int = typer.Option(30, '--before-days', min=0, help='Удалять протухшие раньше N дней назад'),
) -> None:
    """Удаляет ссылки, срок действия которых истёк раньше указанного момента."""

    async def main() -> int:
        cutoff = datetime.now(UTC) - timedelta(days=before_days)
        async with async_session() as session:
            removed = await LinkService(session).purge_expired(before=cutoff)
        await engine.dispose()
        typer.echo(f'Удалено ссылок: {removed} (протухших раньше {cutoff.isoformat()})')
        return 0

    raise typer.Exit(code=asyncio.run(main()))


if __name__ == '__main__':
    cli()
