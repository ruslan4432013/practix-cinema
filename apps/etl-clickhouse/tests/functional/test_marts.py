"""Витрины отвечают на два вопроса задания.

Тест синтетический и намеренно неравномерный: три фильма с разной судьбой
зрителя. Проверяется не «запрос выполняется», а что порядок и точка выхода
совпадают с заложенными в данные.
"""

import uuid

import helpers

DURATION_MS = 120_000
# Шаг тика — 10 % длительности, чтобы бакеты прогресса ложились ровно.
TICK_MS = DURATION_MS // 10


async def _watch(send_event, film_id: str, session: str, ticks: int, finish: bool = False) -> None:
    """Имитирует один сеанс просмотра: ticks меток прогресса и, может быть, досмотр."""
    for index in range(ticks):
        await send_event(
            helpers.video_progress(
                film_id=film_id,
                position_ms=index * TICK_MS,
                duration_ms=DURATION_MS,
                session_id=session,
            )
        )
    if finish:
        await send_event(
            helpers.video_completed(
                film_id=film_id,
                duration_ms=DURATION_MS,
                watched_ms=DURATION_MS,
                session_id=session,
            )
        )


class TestTopFilms:
    async def test_most_watched_films_are_ranked_by_sessions_not_events(self, send_event, wait_for):
        """Популярность считается сеансами, а не событиями.

        Ловушка, ради которой тест написан: у «популярного» фильма меньше
        событий на зрителя, чем у «длинного», и count(event) поставил бы их
        в обратном порядке.
        """
        popular = str(uuid.uuid4())  # 3 зрителя по 2 тика  = 6 событий
        long_tail = str(uuid.uuid4())  # 1 зритель  по 9 тиков = 9 событий

        for _ in range(3):
            await _watch(send_event, popular, f'sess-{uuid.uuid4()}', ticks=2)
        await _watch(send_event, long_tail, f'sess-{uuid.uuid4()}', ticks=9)

        rows = await wait_for(
            f"""SELECT film_id, uniqMerge(views) AS views
                FROM ugc.film_views_daily
                WHERE film_id IN ('{popular}', '{long_tail}')
                GROUP BY film_id ORDER BY views DESC""",
            lambda result: len(result) == 2 and sum(row[1] for row in result) == 4,
            timeout=60,
        )
        ranking = [str(row[0]) for row in rows]
        assert ranking[0] == popular, 'три зрителя популярнее одного, сколько бы тиков он ни прислал'
        assert {str(f): v for f, v in rows} == {popular: 3, long_tail: 1}


class TestRetentionCurve:
    async def test_drop_off_point_is_visible_in_the_curve(self, send_event, wait_for):
        """Кривая досмотра показывает, где именно зрители уходят.

        Заложено: все три зрителя доходят до 30 %, дальше остаётся один.
        Ответить на это по video_completed невозможно в принципе — у
        брошенного просмотра такого события просто нет.
        """
        film_id = str(uuid.uuid4())
        await _watch(send_event, film_id, f'sess-{uuid.uuid4()}', ticks=4)  # до 30 %
        await _watch(send_event, film_id, f'sess-{uuid.uuid4()}', ticks=4)  # до 30 %
        await _watch(send_event, film_id, f'sess-{uuid.uuid4()}', ticks=10, finish=True)  # до конца

        rows = await wait_for(
            f"""SELECT progress_pct, uniqMerge(views) AS views
                FROM ugc.film_retention_daily WHERE film_id = '{film_id}'
                GROUP BY progress_pct ORDER BY progress_pct""",
            lambda result: len(result) == 10,
            timeout=60,
        )
        curve = dict(rows)

        assert curve[0] == 3 and curve[30] == 3, 'до 30 % досматривают все трое'
        assert curve[40] == 1, 'после 30 % остаётся один зритель — это и есть точка выхода'
        assert curve[90] == 1

    async def test_abandon_rate_separates_finished_from_dropped(self, send_event, wait_for):
        """Доля брошенных просмотров: начали трое, досмотрел один."""
        film_id = str(uuid.uuid4())
        await _watch(send_event, film_id, f'sess-{uuid.uuid4()}', ticks=3)
        await _watch(send_event, film_id, f'sess-{uuid.uuid4()}', ticks=3)
        await _watch(send_event, film_id, f'sess-{uuid.uuid4()}', ticks=10, finish=True)

        rows = await wait_for(
            f"""SELECT uniqIfMerge(starts) AS starts, uniqIfMerge(finishes) AS finishes
                FROM ugc.film_completion_daily WHERE film_id = '{film_id}'
                GROUP BY film_id""",
            lambda result: len(result) == 1 and result[0][0] == 3,
            timeout=60,
        )
        starts, finishes = rows[0]
        assert starts == 3, 'начавшим считается тот, у кого есть метка в первых 5 %'
        assert finishes == 1, 'досмотревшим — тот, кто дошёл до 90 %'
        assert round(1 - finishes / starts, 2) == 0.67
