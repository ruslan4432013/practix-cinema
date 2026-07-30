"""Отравленные сообщения не останавливают конвейер.

Классический способ похоронить ETL — упасть на битом сообщении: процесс
перезапускается, читает тот же оффсет, падает снова, и поток стоит навсегда.
Здесь любое нераспознанное сообщение уезжает в карантин ugc.invalid_events, а
оффсет коммитится вместе с пачкой.
"""

import json
import uuid

import helpers


class TestQuarantine:
    async def test_malformed_json_is_quarantined(self, send_raw, wait_for):
        rows = await _quarantine_after(
            send_raw,
            wait_for,
            'ugc.clicks.v1',
            b'{not a json at all',
        )
        assert rows[0][0] == 'json_decode'

    async def test_valid_json_without_event_id_is_quarantined(self, send_raw, wait_for):
        body = helpers.click()
        body.pop('event_id')
        rows = await _quarantine_after(
            send_raw,
            wait_for,
            'ugc.clicks.v1',
            json.dumps(body).encode('utf-8'),
        )
        assert rows[0][0] == 'schema'

    async def test_unknown_event_type_is_quarantined(self, send_raw, wait_for):
        """Новый тип события, выкаченный в коллекторе раньше ETL, не роняет поток."""
        body = helpers.click()
        body['event_type'] = 'some_future_event'
        rows = await _quarantine_after(
            send_raw,
            wait_for,
            'ugc.clicks.v1',
            json.dumps(body).encode('utf-8'),
        )
        assert rows[0][0] == 'unknown_event_type'

    async def test_newer_schema_version_is_quarantined_not_silently_parsed(self, send_raw, wait_for):
        """Конверт версии 99 не разбирается по правилам версии 1.

        Без этой проверки переименованное в v2 поле легло бы в хранилище
        молча испорченным, и заметили бы это только по кривым отчётам.
        """
        body = helpers.click()
        body['schema_version'] = 99
        rows = await _quarantine_after(
            send_raw,
            wait_for,
            'ugc.clicks.v1',
            json.dumps(body).encode('utf-8'),
        )
        assert rows[0][0] == 'schema_version'


class TestPipelineSurvives:
    async def test_good_event_after_poison_pill_still_arrives(self, send_raw, send_event, wait_for):
        """Главная проверка: битое сообщение не блокирует следующие за ним."""
        await send_raw('ugc.video_progress.v1', b'\x00\x01 not even utf-8 {')

        film_id = str(uuid.uuid4())
        body = helpers.video_progress(film_id=film_id)
        await send_event(body)

        rows = await wait_for(
            f"SELECT count() FROM ugc.film_views WHERE film_id = '{film_id}'",
            lambda result: result[0][0] == 1,
        )
        assert rows[0][0] == 1

    async def test_quarantine_is_reflected_in_metrics(self, send_raw, wait_for, metric_value):
        await send_raw('ugc.clicks.v1', b'{broken')
        await wait_for(
            "SELECT count() FROM ugc.invalid_events WHERE error_kind = 'json_decode'",
            lambda result: result[0][0] >= 1,
        )
        assert metric_value('etl_ch_messages_invalid_total') >= 1


async def _quarantine_after(send_raw, wait_for, topic: str, value: bytes):
    """Отправляет сырые байты и дожидается строки в карантине."""
    await send_raw(topic, value)
    return await wait_for(
        f"SELECT error_kind, raw_value FROM ugc.invalid_events WHERE kafka_topic = '{topic}'",
        lambda result: len(result) >= 1,
    )
