"""Метрики ETL.

Два принципиальных отличия от ``analytics_collector/src/core/metrics.py``:

1. **Multiprocess-режим не используется и не должен использоваться.** Коллектор
   работает в четырёх воркерах uvicorn и потому вынужден агрегировать метрики
   через ``PROMETHEUS_MULTIPROC_DIR``. ETL — один процесс с одним event loop
   (параллелизм даётся партициями Kafka и числом контейнеров, а не потоками),
   и multiproc здесь не просто лишний: он ломает ProcessCollector и
   GCCollector, то есть ровно те коллекторы, ради которых всё и затевалось.

2. **Регистрируются встроенные коллекторы процесса.** ``ProcessCollector`` даёт
   ``process_resident_memory_bytes`` — основную метрику памяти, требуемую
   заданием, — читая ``/proc/self/statm`` и не требуя psutil.

Кардинальность. Ни одна метка не содержит ``film_id``, ``event_id``,
``session_id`` и вообще ничего пользовательского: такая метка превратила бы
Prometheus в хранилище событий и убила бы его за часы.
"""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.gc_collector import GCCollector
from prometheus_client.platform_collector import PlatformCollector
from prometheus_client.process_collector import ProcessCollector

# Свой реестр вместо глобального REGISTRY: так в /metrics попадает только то,
# что мы объявили, и импорт сторонней библиотеки не добавляет туда мусор.
registry = CollectorRegistry()

ProcessCollector(registry=registry)  # process_resident_memory_bytes, _virtual_, open_fds
GCCollector(registry=registry)  # python_gc_objects_collected_total, python_gc_collections_total
PlatformCollector(registry=registry)

# --- Поток данных ---------------------------------------------------------
messages_consumed = Counter(
    'etl_ch_messages_consumed_total', 'Сообщений прочитано из Kafka', ['topic'], registry=registry
)
rows_inserted = Counter('etl_ch_rows_inserted_total', 'Строк записано в ClickHouse', ['table'], registry=registry)
messages_invalid = Counter(
    'etl_ch_messages_invalid_total',
    'Сообщений отправлено в карантин ugc.invalid_events',
    ['topic', 'reason'],
    registry=registry,
)
messages_duplicate = Counter('etl_ch_messages_duplicate_total', 'Дубликатов схлопнуто внутри пачки', registry=registry)

# --- Здоровье конвейера ---------------------------------------------------
batches_total = Counter('etl_ch_batches_total', 'Обработанных пачек', ['result'], registry=registry)
flush_total = Counter('etl_ch_flush_total', 'Сбросов буфера по причине закрытия пачки', ['reason'], registry=registry)
insert_duration = Histogram(
    'etl_ch_insert_duration_seconds',
    'Длительность вставки в ClickHouse',
    ['table'],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
    registry=registry,
)
batch_rows = Histogram(
    'etl_ch_batch_rows',
    'Размер пачки в строках',
    buckets=(10, 50, 100, 500, 1_000, 5_000, 10_000, 50_000),
    registry=registry,
)
batch_bytes = Histogram(
    'etl_ch_batch_bytes',
    'Размер пачки в байтах сырых сообщений',
    buckets=(1_024, 16_384, 262_144, 1_048_576, 8_388_608, 33_554_432, 134_217_728),
    registry=registry,
)
commit_failures = Counter(
    'etl_ch_commit_failures_total', 'Неудачных коммитов оффсетов (обычно ребаланс)', registry=registry
)
rebalances = Counter('etl_ch_rebalances_total', 'Ребалансов группы', ['phase'], registry=registry)

# --- Состояние ------------------------------------------------------------
consumer_lag = Gauge(
    'etl_ch_consumer_lag', 'Отставание консьюмера, сообщений', ['topic', 'partition'], registry=registry
)
consumer_lag_total = Gauge('etl_ch_consumer_lag_total', 'Суммарное отставание консьюмера', registry=registry)
clickhouse_up = Gauge('etl_ch_clickhouse_up', 'ClickHouse доступен (1/0)', registry=registry)
kafka_up = Gauge('etl_ch_kafka_up', 'Kafka доступна (1/0)', registry=registry)
# >0 означает, что включён backpressure: чтение остановлено, пока приёмник
# недоступен. Главный признак того, что данные копятся в Kafka, а не теряются.
paused_partitions = Gauge('etl_ch_paused_partitions', 'Партиций на паузе', registry=registry)
last_flush_timestamp = Gauge('etl_ch_last_flush_timestamp', 'Unix-время последней успешной вставки', registry=registry)

# --- Память ---------------------------------------------------------------
# sys.getallocatedblocks(): число живых блоков CPython. Не зависит от поведения
# аллокатора (в отличие от RSS) и растёт монотонно ровно тогда, когда объекты
# действительно не освобождаются, — лучший дешёвый индикатор утечки.
allocated_blocks = Gauge(
    'etl_ch_allocated_blocks', 'Живых блоков памяти CPython (sys.getallocatedblocks)', registry=registry
)
tracemalloc_traced_bytes = Gauge(
    'etl_ch_tracemalloc_traced_bytes', 'Объём отслеживаемых tracemalloc аллокаций', registry=registry
)
tracemalloc_peak_bytes = Gauge(
    'etl_ch_tracemalloc_peak_bytes', 'Пик отслеживаемых tracemalloc аллокаций', registry=registry
)
