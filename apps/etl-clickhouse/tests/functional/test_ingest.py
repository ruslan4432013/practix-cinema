"""Доставка событий из Kafka в ClickHouse."""

import uuid

import helpers


class TestRawEvents:
    async def test_every_event_family_lands_in_raw_events(self, send_event, wait_for):
        """Все шесть семейств событий доезжают до сырой таблицы."""
        bodies = [
            helpers.click(),
            helpers.page_view(),
            helpers.quality_change(),
            helpers.video_progress(),
            helpers.video_completed(),
            helpers.search_filter(),
        ]
        for body in bodies:
            await send_event(body)

        ids = "','".join(str(body['event_id']) for body in bodies)
        rows = await wait_for(
            f"SELECT event_type FROM ugc.raw_events WHERE event_id IN ('{ids}')",
            lambda result: len(result) == len(bodies),
        )
        assert {row[0] for row in rows} == {
            'click',
            'page_view',
            'video_quality_change',
            'video_progress',
            'video_completed',
            'search_filter_used',
        }

    async def test_envelope_fields_are_preserved(self, send_event, wait_for):
        """Конверт раскладывается по колонкам без потерь, payload — сырым."""
        body = helpers.video_progress(position_ms=42_000)
        await send_event(body)

        rows = await wait_for(
            f"""SELECT session_id, anonymous_id, partition_key, url, device_type, browser,
                       ip_hash, schema_version, kafka_topic,
                       JSONExtractInt(payload, 'playback_position_ms')
                FROM ugc.raw_events WHERE event_id = '{body['event_id']}'""",
            lambda result: len(result) == 1,
        )
        (
            session_id,
            anonymous_id,
            partition_key,
            url,
            device_type,
            browser,
            ip_hash,
            schema_version,
            topic,
            position,
        ) = rows[0]

        assert session_id == body['session_id']
        assert anonymous_id == body['anonymous_id']
        # У анонима ключом партиционирования служит anonymous_id.
        assert partition_key == body['anonymous_id']
        assert url == body['context']['url']
        assert device_type == 'desktop'
        assert browser == 'Chrome'
        assert ip_hash == body['context']['ip_hash']
        assert schema_version == 1
        assert topic == 'ugc.video_progress.v1'
        # payload сохранён целиком, а не только разобранные поля.
        assert position == 42_000

    async def test_dlq_message_keeps_its_original_topic(self, send_event, wait_for):
        """Событие из DLQ помнит, откуда оно приехало.

        В kafka_topic у такого сообщения стоит DLQ, и без origin_topic
        восстановить исходный поток было бы невозможно.
        """
        body = helpers.click()
        await send_event(
            body,
            topic='ugc.events.dlq.v1',
            headers=[
                ('event_type', b'click'),
                ('event_id', str(body['event_id']).encode()),
                ('schema_version', b'1'),
                ('x-original-topic', b'ugc.clicks.v1'),
            ],
        )

        rows = await wait_for(
            f"SELECT kafka_topic, origin_topic FROM ugc.raw_events WHERE event_id = '{body['event_id']}'",
            lambda result: len(result) == 1,
        )
        assert rows[0] == ('ugc.events.dlq.v1', 'ugc.clicks.v1')


class TestFilmViews:
    async def test_video_progress_is_typed_into_film_views(self, send_event, wait_for):
        """Метка прогресса раскладывается в типизированную таблицу просмотров."""
        film_id = str(uuid.uuid4())
        body = helpers.video_progress(film_id=film_id, position_ms=30_000, duration_ms=120_000)
        await send_event(body)

        rows = await wait_for(
            f"""SELECT view_id, completion_rate, progress_pct, playback_position_ms, duration_ms, quality
                FROM ugc.film_views WHERE film_id = '{film_id}'""",
            lambda result: len(result) == 1,
        )
        view_id, completion_rate, progress_pct, position, duration, quality = rows[0]

        # Сеанс просмотра, а не событие: иначе 240 тиков дали бы 240 «просмотров».
        assert view_id == f'{body["session_id"]}:{film_id}'
        assert abs(completion_rate - 0.25) < 1e-4
        # Бакет с шагом 5 %, посчитанный на этапе ETL.
        assert progress_pct == 25
        assert position == 30_000
        assert duration == 120_000
        assert quality == '1080p'

    async def test_all_ticks_of_one_session_share_one_view_id(self, send_event, wait_for):
        """Десяток тиков одного сеанса — это один просмотр, а не десять."""
        film_id = str(uuid.uuid4())
        session = f'sess-{uuid.uuid4()}'
        positions = [0, 12_000, 24_000, 36_000, 48_000, 60_000]
        for position in positions:
            await send_event(
                helpers.video_progress(
                    film_id=film_id,
                    position_ms=position,
                    duration_ms=120_000,
                    session_id=session,
                )
            )

        rows = await wait_for(
            f"""SELECT uniq(view_id), uniq(event_id), groupArray(progress_pct)
                FROM ugc.film_views WHERE film_id = '{film_id}'""",
            lambda result: result and result[0][1] == len(positions),
        )
        unique_views, unique_events, buckets = rows[0]
        assert unique_views == 1, 'все тики одного сеанса образуют один просмотр'
        assert unique_events == len(positions)
        assert sorted(buckets) == [0, 10, 20, 30, 40, 50]

    async def test_quality_change_does_not_create_a_zero_progress_row(self, ch, send_event, wait_for):
        """Смена качества в витрину не попадает.

        У события нет duration_ms, поэтому строка витрины получила бы
        progress_pct = 0 — то есть «зритель не сдвинулся с начала», чего событие
        не утверждает. На uniq(view_id) это не влияло бы, но любое среднее по
        прогрессу без фильтра по event_type тянуло бы к нулю. Само событие при
        этом сохраняется целиком — в сырой таблице.
        """
        film_id = str(uuid.uuid4())
        session = f'sess-{uuid.uuid4()}'
        change = helpers.quality_change(film_id=film_id, session_id=session)
        await send_event(change)

        await wait_for(
            f"SELECT count() FROM ugc.raw_events WHERE event_id = '{change['event_id']}'",
            lambda result: result[0][0] >= 1,
        )

        views = await ch.query(f"SELECT count() FROM ugc.film_views WHERE film_id = '{film_id}'")
        assert views.result_rows[0][0] == 0

        # А позиция переключения не потеряна — она в payload сырого события.
        raw = await ch.query(
            f"""SELECT JSONExtractInt(payload, 'playback_position_ms'),
                       JSONExtractString(payload, 'to_quality')
                FROM ugc.raw_events WHERE event_id = '{change['event_id']}'"""
        )
        assert raw.result_rows[0] == (12_000, '1080p')

    async def test_non_film_events_do_not_reach_film_views(self, ch, send_event, wait_for):
        """Клики и поиск в таблице просмотров не появляются."""
        click = helpers.click()
        search = helpers.search_filter()
        await send_event(click)
        await send_event(search)

        # Дожидаемся обоих в сырой таблице — значит пачка уже обработана.
        await wait_for(
            f"""SELECT count() FROM ugc.raw_events
                WHERE event_id IN ('{click['event_id']}', '{search['event_id']}')""",
            lambda result: result[0][0] == 2,
        )
        views = await ch.query('SELECT count() FROM ugc.film_views')
        assert views.result_rows[0][0] == 0
