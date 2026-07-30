"""Идемпотентность приёмника.

Доставка из Kafka гарантирована «хотя бы один раз»: коллектор ретраит отправку,
дренаж буфера деградации ретраит её ещё раз, а сам ETL может упасть между
вставкой и коммитом оффсетов. Дубликаты будут всегда, и гасить их обязан
приёмник — иначе «топ фильмов» будет считать не популярность, а везучесть.

``TestCrashBetweenInsertAndCommit`` проверяет самый тонкий из трёх уровней
дедупликации — ``insert_deduplication_token``. Он держится на неявном
инварианте: после перезапуска ETL обязан собрать РОВНО ТУ ЖЕ пачку (те же
оффсеты, те же таблицы). Любое изменение логики батчинга — например, разбиение
пачки по размеру между ``raw_events`` и ``film_views`` — сломает дедупликацию
молча, без единой ошибки в логе. Поэтому тест гоняет настоящий ``Runner``, а не
повторяет его арифметику у себя: иначе он проверял бы сам себя.
"""

import asyncio
import contextlib
import uuid

import helpers
import pytest_asyncio
from aiokafka import AIOKafkaConsumer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from conftest import KAFKA_BOOTSTRAP

from practix_etl_clickhouse.pipeline.buffer import BatchBuffer
from practix_etl_clickhouse.pipeline.runner import Runner
from practix_etl_clickhouse.sinks.clickhouse import ClickHouseSink, build_dedup_token
from practix_etl_clickhouse.transform.envelope import parse
from practix_etl_clickhouse.transform.raw import RAW_COLUMN_TYPES, RAW_COLUMNS, RAW_TABLE, to_raw_row


class TestDeduplication:
    async def test_same_event_delivered_twice_is_stored_once(self, send_event, wait_for):
        """Повторная доставка того же event_id не задваивает данные."""
        body = helpers.video_progress(film_id=str(uuid.uuid4()))
        await send_event(body)
        await wait_for(
            f"SELECT count() FROM ugc.raw_events WHERE event_id = '{body['event_id']}'",
            lambda result: result[0][0] >= 1,
        )
        # Второй раз — то же самое сообщение целиком, как при ретрае коллектора.
        await send_event(body)

        rows = await wait_for(
            f"""SELECT count(), uniq(event_id) FROM ugc.raw_events
                WHERE event_id = '{body['event_id']}'""",
            lambda result: result[0][0] >= 1,
            timeout=15,
        )
        _, unique = rows[0]
        # uniq не зависит от того, успело ли пройти слияние ReplacingMergeTree,
        # поэтому именно на нём построены все витрины.
        assert unique == 1

        final = await wait_for(
            f"SELECT count() FROM ugc.raw_events FINAL WHERE event_id = '{body['event_id']}'",
            lambda result: result[0][0] == 1,
            timeout=30,
        )
        assert final[0][0] == 1

    async def test_duplicates_do_not_inflate_the_top_films_mart(self, send_event, wait_for):
        """Витрина топа не завышается от повторной доставки.

        Это и есть причина, по которой все счётчики витрин построены на uniq,
        а не на count: материализованное представление срабатывает на
        вставляемый блок, и count посчитал бы дубль вторым просмотром.
        """
        film_id = str(uuid.uuid4())
        session = f'sess-{uuid.uuid4()}'
        body = helpers.video_progress(film_id=film_id, session_id=session)

        for _ in range(3):
            await send_event(body)

        rows = await wait_for(
            f"""SELECT uniqMerge(views) FROM ugc.film_views_daily
                WHERE film_id = '{film_id}' GROUP BY film_id""",
            lambda result: len(result) == 1,
        )
        assert rows[0][0] == 1, 'три доставки одного события — это один просмотр'


@pytest_asyncio.fixture
async def crash_topic():
    """Отдельный топик под сценарий падения.

    Свой топик, а не общий: тест сам выступает потребителем и вставляет пачку в
    хранилище, поэтому работающий рядом контейнер ETL не должен читать те же
    сообщения — иначе строки в ClickHouse появлялись бы из двух источников и
    счёт дублей стал бы бессмысленным.
    """
    admin = AIOKafkaAdminClient(bootstrap_servers=KAFKA_BOOTSTRAP.split(','))
    await admin.start()
    name = f'ugc.test.crash.{uuid.uuid4().hex[:8]}.v1'
    try:
        await admin.create_topics([NewTopic(name, num_partitions=1, replication_factor=1)])
    finally:
        await admin.close()

    yield name

    admin = AIOKafkaAdminClient(bootstrap_servers=KAFKA_BOOTSTRAP.split(','))
    await admin.start()
    with contextlib.suppress(Exception):
        await admin.delete_topics([name])
    await admin.close()


class _CommitCrashes:
    """Консьюмер, чей коммит не доживает до конца.

    Так выглядит авария, ради которой существует ``insert_deduplication_token``:
    вставка в ClickHouse уже прошла, а оффсеты подтвердить процесс не успел.
    """

    def __init__(self, consumer):
        self._consumer = consumer

    def __getattr__(self, name):
        return getattr(self._consumer, name)

    async def commit(self, offsets=None):
        raise RuntimeError('процесс упал между вставкой и коммитом')


async def _read_batch(topic: str, group: str, expected: int, timeout: float = 30.0) -> tuple:
    """Читает ``expected`` сообщений в буфер ETL, ничего не коммитя."""
    consumer = AIOKafkaConsumer(
        topic,
        bootstrap_servers=KAFKA_BOOTSTRAP.split(','),
        group_id=group,
        enable_auto_commit=False,
        auto_offset_reset='earliest',
    )
    await consumer.start()
    buffer = BatchBuffer()
    deadline = asyncio.get_running_loop().time() + timeout
    while buffer.rows < expected and asyncio.get_running_loop().time() < deadline:
        for tp, messages in (await consumer.getmany(timeout_ms=1000)).items():
            for message in messages:
                buffer.add(tp, message)
    if buffer.rows < expected:
        await consumer.stop()
        raise AssertionError(f'прочитано {buffer.rows} из {expected} сообщений топика {topic}')
    return consumer, buffer


class TestCrashBetweenInsertAndCommit:
    async def test_restart_after_insert_does_not_duplicate(self, ch, producer, crash_topic, wait_for):
        """Упали между insert и commit → перезапустились → дублей нет.

        Инвариант, который проверяется: перезапущенный ETL читает те же оффсеты,
        собирает ту же пачку и получает тот же ``insert_deduplication_token``,
        поэтому ClickHouse отбрасывает повторную вставку целиком.
        """
        film_id = str(uuid.uuid4())
        session = f'sess-{uuid.uuid4()}'
        bodies = [
            helpers.video_progress(film_id=film_id, position_ms=position, duration_ms=120_000, session_id=session)
            for position in (0, 30_000, 60_000)
        ]
        for body in bodies:
            await producer.send_and_wait(
                crash_topic,
                value=helpers.encode(body),
                key=helpers.partition_key(body).encode('utf-8'),
            )

        group = f'crash-test-{uuid.uuid4().hex[:8]}'
        sink = ClickHouseSink()
        await sink.start()
        try:
            # --- Жизнь первая: вставка прошла, коммит — нет.
            consumer, buffer = await _read_batch(crash_topic, group, len(bodies))
            runner = Runner(_CommitCrashes(consumer), sink, buffer, asyncio.Event())
            with contextlib.suppress(RuntimeError):
                await runner.flush('crash')
            await consumer.stop()

            # --- Жизнь вторая: оффсеты не подтверждены, значит те же сообщения
            # читаются заново — ровно как после реального перезапуска.
            consumer, buffer = await _read_batch(crash_topic, group, len(bodies))
            assert buffer.rows == len(bodies), 'перезапуск обязан перечитать всю непрокоммиченную пачку'
            runner = Runner(consumer, sink, buffer, asyncio.Event())
            await runner.flush('restart')
            await consumer.stop()
        finally:
            await sink.close()

        ids = "','".join(str(body['event_id']) for body in bodies)
        rows = await wait_for(
            f"SELECT count(), uniq(event_id) FROM ugc.raw_events WHERE event_id IN ('{ids}')",
            lambda result: result[0][1] == len(bodies),
        )
        count, unique = rows[0]
        assert unique == len(bodies)
        assert count == len(bodies), 'повторная вставка той же пачки должна быть отброшена ClickHouse'

        views = await ch.query(f"SELECT count(), uniq(view_id) FROM ugc.film_views WHERE film_id = '{film_id}'")
        assert views.result_rows[0] == (len(bodies), 1), 'витрина просмотров тоже не должна задвоиться'

    async def test_batching_change_would_bring_duplicates_back(self, ch, wait_for):
        """Показывает, на чём именно держится идемпотентность.

        Токен считается из диапазонов оффсетов пачки. Стоит после перезапуска
        собрать пачку иначе — разбить по размеру, поменять порядок таблиц, — и
        токен станет другим, а ClickHouse примет ту же строку второй раз. Тест
        воспроизводит именно это: те же данные, другой токен → дубликат.
        Он и есть сигнализация: если правка батчинга ломает дедупликацию,
        падает тест, а не дашборд аналитика через полгода.
        """
        body = helpers.video_progress(film_id=str(uuid.uuid4()))
        parsed = parse(helpers.encode(body))
        row = to_raw_row(
            parsed, topic='ugc.video_progress.v1', origin_topic='ugc.video_progress.v1', partition=0, offset=7
        )

        sink = ClickHouseSink()
        await sink.start()
        try:
            for ranges in ({('ugc.video_progress.v1', 0): (5, 9)}, {('ugc.video_progress.v1', 0): (7, 9)}):
                await sink.insert(
                    RAW_TABLE,
                    [row],
                    RAW_COLUMNS,
                    RAW_COLUMN_TYPES,
                    build_dedup_token(RAW_TABLE, ranges),
                )
        finally:
            await sink.close()

        rows = await wait_for(
            f"SELECT count(), uniq(event_id) FROM ugc.raw_events WHERE event_id = '{body['event_id']}'",
            lambda result: result[0][0] >= 2,
            timeout=15,
        )
        assert rows[0] == (2, 1), 'другой токен — другая пачка для ClickHouse, дедупликация не срабатывает'
