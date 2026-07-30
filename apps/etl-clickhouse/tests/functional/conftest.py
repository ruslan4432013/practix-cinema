"""Фикстуры функциональных тестов ETL.

Тесты чёрного ящика: продюсер кладёт сообщение в настоящую Kafka, ETL работает
отдельным контейнером, проверка — SELECT в настоящем ClickHouse. Никаких
внутренностей сервиса тесты не знают, кроме имён топиков и таблиц.

Читаем ClickHouse НАПРЯМУЮ, минуя toxiproxy: тест должен видеть хранилище даже
тогда, когда для ETL оно «сломано».
"""

import asyncio
import json
import os
import urllib.request

import clickhouse_connect
import pytest
import pytest_asyncio
from aiokafka import AIOKafkaProducer

CH_HOST = os.environ.get('CH_HOST', 'clickhouse')
CH_PORT = int(os.environ.get('CH_PORT', '8123'))
CH_USER = os.environ.get('CH_USER', 'etl')
CH_PASSWORD = os.environ.get('CH_PASSWORD', 'etl')
CH_DATABASE = os.environ.get('CH_DATABASE', 'ugc')

KAFKA_BOOTSTRAP = os.environ.get('KAFKA_BOOTSTRAP_SERVERS', 'kafka:9092')
ETL_METRICS_URL = os.environ.get('ETL_METRICS_URL', 'http://etl-clickhouse:8000/metrics')
TOXIPROXY_URL = os.environ.get('TOXIPROXY_URL', 'http://toxiproxy:8474')

TOPICS = {
    'click': os.environ.get('KAFKA_TOPIC_CLICKS', 'ugc.clicks.v1'),
    'page_view': os.environ.get('KAFKA_TOPIC_PAGE_VIEWS', 'ugc.page_views.v1'),
    'video_quality_change': os.environ.get('KAFKA_TOPIC_VIDEO_EVENTS', 'ugc.video_events.v1'),
    'video_completed': os.environ.get('KAFKA_TOPIC_VIDEO_EVENTS', 'ugc.video_events.v1'),
    'video_progress': os.environ.get('KAFKA_TOPIC_VIDEO_PROGRESS', 'ugc.video_progress.v1'),
    'search_filter_used': os.environ.get('KAFKA_TOPIC_SEARCH_EVENTS', 'ugc.search_events.v1'),
}
DLQ_TOPIC = os.environ.get('KAFKA_TOPIC_DLQ', 'ugc.events.dlq.v1')

# Локальные таблицы, а не Distributed: TRUNCATE поверх Distributed не
# поддерживается.
_LOCAL_TABLES = (
    'raw_events_local',
    'film_views_local',
    'invalid_events_local',
    'film_views_daily_local',
    'film_retention_daily_local',
    'film_completion_daily_local',
)


@pytest_asyncio.fixture
async def ch():
    client = await clickhouse_connect.get_async_client(
        host=CH_HOST,
        port=CH_PORT,
        username=CH_USER,
        password=CH_PASSWORD,
        database=CH_DATABASE,
    )
    for table in _LOCAL_TABLES:
        await client.command(f'TRUNCATE TABLE IF EXISTS {CH_DATABASE}.{table}')
    yield client
    await client.close()


@pytest_asyncio.fixture
async def producer():
    instance = AIOKafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP.split(','),
        acks='all',
        # Тот же кодек, что у коллектора: если ETL соберут без cramjam, тест
        # должен это поймать.
        compression_type='lz4',
    )
    await instance.start()
    yield instance
    await instance.stop()


@pytest.fixture
def send_event(producer):
    """Отправляет конверт в топик, соответствующий его типу события."""
    from helpers import encode, partition_key

    async def _send(body: dict, topic: str | None = None, headers=None):
        target = topic or TOPICS[body['event_type']]
        await producer.send_and_wait(
            target,
            value=encode(body),
            key=partition_key(body).encode('utf-8'),
            headers=headers
            or [
                ('event_type', body['event_type'].encode('utf-8')),
                ('event_id', str(body['event_id']).encode('utf-8')),
                ('schema_version', str(body['schema_version']).encode('utf-8')),
            ],
        )
        return body['event_id']

    return _send


@pytest.fixture
def send_raw(producer):
    """Отправляет произвольные байты — для проверки отравленных сообщений."""

    async def _send(topic: str, value: bytes, key: bytes = b'poison'):
        await producer.send_and_wait(topic, value=value, key=key)

    return _send


@pytest.fixture
def wait_for(ch):
    """Опрашивает ClickHouse, пока условие не выполнится.

    ETL асинхронный: между отправкой в Kafka и появлением строки проходит до
    ETL_FLUSH_INTERVAL плюс время вставки. Проверять «сразу после отправки»
    нельзя — тест был бы flaky по построению.
    """

    async def _wait(query: str, predicate, timeout: float = 30.0, interval: float = 0.5):
        deadline = asyncio.get_running_loop().time() + timeout
        last = None
        while asyncio.get_running_loop().time() < deadline:
            result = await ch.query(query)
            last = result.result_rows
            if predicate(last):
                return last
            await asyncio.sleep(interval)
        raise AssertionError(f'condition not met in {timeout}s for query:\n{query}\nlast result: {last}')

    return _wait


@pytest.fixture
def etl_metrics():
    """Текст /metrics ETL."""

    def _fetch() -> str:
        with urllib.request.urlopen(ETL_METRICS_URL, timeout=5) as response:
            return response.read().decode('utf-8')

    return _fetch


@pytest.fixture
def metric_value(etl_metrics):
    """Значение метрики без меток (или сумма по всем меткам)."""

    def _value(name: str) -> float:
        total = 0.0
        found = False
        for line in etl_metrics().splitlines():
            if line.startswith('#') or not line.startswith(name):
                continue
            head, _, raw = line.rpartition(' ')
            metric_name = head.split('{', 1)[0].strip()
            if metric_name != name:
                continue
            found = True
            total += float(raw)
        return total if found else float('nan')

    return _value


@pytest.fixture
def toxiproxy():
    """Управление отказом ClickHouse для ETL.

    Toxiproxy, а не `docker compose stop`: не нужен доступ к docker-сокету из
    контейнера тестов и нет зависимости от имени compose-проекта.
    """

    def _request(method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode('utf-8') if body is not None else None
        request = urllib.request.Request(
            f'{TOXIPROXY_URL}{path}',
            data=data,
            method=method,
            headers={'Content-Type': 'application/json'},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = response.read().decode('utf-8')
                return json.loads(payload) if payload else None
        except urllib.error.HTTPError as exc:
            # 404 при удалении несуществующего toxic — нормальный путь очистки.
            if exc.code == 404:
                return None
            raise

    class Toxiproxy:
        name = 'clickhouse'

        def break_storage(self) -> None:
            _request(
                'POST',
                f'/proxies/{self.name}/toxics',
                {
                    'name': 'outage',
                    'type': 'timeout',
                    'stream': 'downstream',
                    'attributes': {'timeout': 1},
                },
            )

        def heal_storage(self) -> None:
            _request('DELETE', f'/proxies/{self.name}/toxics/outage')

    instance = Toxiproxy()
    yield instance
    instance.heal_storage()
