"""Ожидание готовности ClickHouse перед запуском тестов.

По образцу ``wait_for_kafka.py``: проверяется не только «сервер отвечает», но и
готовность схемы. Без второй проверки тесты стартовали бы раньше, чем
одноразовый ``clickhouse-init`` применит DDL, и падали бы на «unknown table».

Реализовано на голом ``urllib.request``: скрипт запускается в образе ETL, но
может понадобиться и в любом другом, где драйвера ClickHouse нет.
"""

import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HOST = os.environ.get('CH_HOST', 'clickhouse')
PORT = os.environ.get('CH_PORT', '8123')
USER = os.environ.get('CH_USER', 'etl')
PASSWORD = os.environ.get('CH_PASSWORD', 'etl')
DATABASE = os.environ.get('CH_DATABASE', 'ugc')

QUERIES = (
    'SELECT 1',
    f'EXISTS TABLE {DATABASE}.raw_events',
    f'EXISTS TABLE {DATABASE}.film_views',
    f'EXISTS TABLE {DATABASE}.film_views_daily',
    f'EXISTS TABLE {DATABASE}.film_retention_daily',
    f'EXISTS TABLE {DATABASE}.film_completion_daily',
    f'EXISTS TABLE {DATABASE}.invalid_events',
)

MAX_ATTEMPTS = 30
DELAY_SECONDS = 2


def run_query(query: str) -> str:
    url = f'http://{HOST}:{PORT}/?' + urllib.parse.urlencode({'query': query})
    request = urllib.request.Request(url)
    request.add_header('X-ClickHouse-User', USER)
    request.add_header('X-ClickHouse-Key', PASSWORD)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.read().decode('utf-8').strip()


def wait_for_clickhouse() -> None:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            for query in QUERIES:
                result = run_query(query)
                # EXISTS TABLE возвращает 0 или 1; SELECT 1 — единицу.
                if result != '1':
                    raise RuntimeError(f'{query!r} returned {result!r}')
            print(f'ClickHouse is ready at {HOST}:{PORT}, schema {DATABASE} is applied')
            return
        except (urllib.error.URLError, OSError, RuntimeError) as exc:
            print(f'[{attempt}/{MAX_ATTEMPTS}] ClickHouse is not ready yet: {exc}')
        time.sleep(DELAY_SECONDS)

    raise SystemExit(f'ClickHouse at {HOST}:{PORT} did not become ready in time')


if __name__ == '__main__':
    wait_for_clickhouse()
    sys.exit(0)
