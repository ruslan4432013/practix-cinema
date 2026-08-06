"""Обслуживание преагрегата рейтингов.

Зачем это нужно. ``film_rating`` — денормализация: те же данные, что в
``likes``, только посчитанные заранее. Любая денормализация рано или поздно
расходится с источником — из-за ошибки в коде, ручного вмешательства или
восстановления из бэкапа не того момента. Риск-таблица исследования отвечает на
это «периодический пересчёт плюс метрика расхождения»; здесь — обе половины,
кроме экспорта в Prometheus (см. README сервиса, раздел «не сделано»).

Запуск:

    python -m practix_ugc_api.cli recount           # пересчитать
    python -m practix_ugc_api.cli recount --check   # только сравнить

У Typer-приложения с ОДНОЙ командой имя подкоманды не принимается — приложение
«схлопывается». В Auth на это уже наступили и с появлением второй команды
починили тем же способом. Здесь схлопывание отключено пустым ``callback``,
поэтому подкоманда пишется явно и добавление второй ничего не сломает.
"""

import asyncio

import typer
from sqlalchemy import text

from practix_ugc_api.db.postgres import async_session, engine
from practix_ugc_api.services.aggregate import HISTOGRAM_SIZE

# Гистограмма собирается одиннадцатью FILTER-агрегатами за один проход по
# таблице. Числа берутся из range(), а не из ввода, поэтому подстановка в текст
# запроса безопасна; корректный порядок позиций гарантирован порядком генерации.
_HISTOGRAM_EXPRESSION = ',\n               '.join(
    f'count(*) FILTER (WHERE rating = {value})::int' for value in range(HISTOGRAM_SIZE)
)

_ACTUAL = f"""
    SELECT film_id,
           count(*)::int AS ratings_count,
           sum(rating)::bigint AS ratings_sum,
           ARRAY[
               {_HISTOGRAM_EXPRESSION}
           ] AS hist
    FROM likes
    GROUP BY film_id
"""

# Пересчёт идёт upsert'ом, а не TRUNCATE + INSERT: TRUNCATE берёт
# ACCESS EXCLUSIVE и на время пересчёта останавливает чтение рейтингов всеми
# пользователями сразу. Обслуживающая команда не должна быть заметнее аварии,
# которую она чинит.
_RECOUNT = text(
    f"""
    INSERT INTO film_rating (film_id, ratings_count, ratings_sum, hist)
    {_ACTUAL}
    ON CONFLICT (film_id) DO UPDATE SET
        ratings_count = EXCLUDED.ratings_count,
        ratings_sum   = EXCLUDED.ratings_sum,
        hist          = EXCLUDED.hist
    """
)

# Фильмы, у которых не осталось ни одной оценки, теряют и строку преагрегата:
# иначе она навсегда осталась бы нулевой висячей записью.
_DROP_ORPHANS = text('DELETE FROM film_rating fr WHERE NOT EXISTS (SELECT 1 FROM likes l WHERE l.film_id = fr.film_id)')

# Отсутствующая строка приравнивается к нулевой — иначе фильм без оценок и без
# строки преагрегата считался бы расхождением, хотя это ровно то состояние,
# которое пересчёт и оставляет.
_DIVERGENCE = text(
    f"""
    WITH actual AS ({_ACTUAL}), merged AS (
        SELECT coalesce(a.ratings_count, 0) AS a_count,
               coalesce(a.ratings_sum, 0) AS a_sum,
               coalesce(a.hist, array_fill(0, ARRAY[{HISTOGRAM_SIZE}])) AS a_hist,
               coalesce(f.ratings_count, 0) AS f_count,
               coalesce(f.ratings_sum, 0) AS f_sum,
               coalesce(f.hist, array_fill(0, ARRAY[{HISTOGRAM_SIZE}])) AS f_hist
        FROM actual a FULL OUTER JOIN film_rating f ON a.film_id = f.film_id
    )
    SELECT count(*) FROM merged
    WHERE a_count <> f_count OR a_sum <> f_sum OR a_hist <> f_hist
    """
)

cli = typer.Typer(help='Обслуживание преагрегата рейтингов UGC-сервиса.')


@cli.callback()
def _root() -> None:
    """Пустой callback: не даёт Typer схлопнуть приложение до одной команды."""


async def _divergent_films() -> int:
    async with async_session() as session:
        return await session.scalar(_DIVERGENCE)


async def _recount() -> None:
    async with async_session() as session, session.begin():
        await session.execute(_RECOUNT)
        await session.execute(_DROP_ORPHANS)


@cli.command()
def recount(
    check: bool = typer.Option(False, '--check', help='Только сравнить, ничего не менять'),
) -> None:
    """Пересчитывает ``film_rating`` из ``likes`` (или проверяет расхождение)."""

    async def main() -> int:
        divergent = await _divergent_films()
        if check:
            typer.echo(f'Расходящихся фильмов: {divergent}')
            await engine.dispose()
            # Ненулевой код — чтобы команду можно было поставить в cron или в
            # проверку CI и узнать о расхождении, не читая вывод.
            return 1 if divergent else 0
        await _recount()
        typer.echo(f'Пересчёт завершён, расходилось фильмов: {divergent}')
        await engine.dispose()
        return 0

    raise typer.Exit(code=asyncio.run(main()))


if __name__ == '__main__':
    cli()
