"""Метрики Prometheus.

Набор подобран так, чтобы по нему можно было ответить на эксплуатационные
вопросы, ради которых сервис вообще мониторят:

* «Теряем ли мы события?» — ``ugc_events_dropped_total`` (обязан быть нулём) и
  соотношение ``ugc_events_buffered_total`` к ``ugc_events_published_total``.
* «Успевает ли ingest?» — гистограмма ``ugc_publish_duration_seconds``.
* «Жива ли Kafka с точки зрения сервиса?» — gauge ``ugc_broker_up``.
* «Не растёт ли буфер деградации?» — gauge ``ugc_fallback_buffer_size``.

Счётчики размечены по типу события: без этой метки невозможно понять, что
сломался конкретный тип события, а не сервис целиком.
"""

import os

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, multiprocess

# Отдельный реестр вместо глобального: так в реестр не попадают дефолтные
# коллекторы, а тесты могут собрать метрики независимо.
registry = CollectorRegistry()

events_received = Counter(
    'ugc_events_received_total',
    'Число принятых событий (до публикации)',
    ['event_type'],
    registry=registry,
)

events_published = Counter(
    'ugc_events_published_total',
    'Число событий, переданных продюсеру Kafka',
    ['event_type', 'topic'],
    registry=registry,
)

events_buffered = Counter(
    'ugc_events_buffered_total',
    'Число событий, ушедших в Redis-буфер из-за недоступности Kafka',
    ['event_type'],
    registry=registry,
)

events_dropped = Counter(
    'ugc_events_dropped_total',
    'Число потерянных событий (недоступны и Kafka, и Redis-буфер)',
    ['event_type', 'reason'],
    registry=registry,
)

events_duplicated = Counter(
    'ugc_events_duplicate_total',
    'Число событий, подавленных дедупликацией по event_id',
    ['event_type'],
    registry=registry,
)

events_filtered = Counter(
    'ugc_events_filtered_total',
    'Число событий, отброшенных фильтром до публикации (трафик ботов)',
    ['event_type', 'reason'],
    registry=registry,
)

events_rejected = Counter(
    'ugc_events_rejected_total',
    'Число запросов, отклонённых валидацией или rate limit',
    ['reason'],
    registry=registry,
)

publish_duration = Histogram(
    'ugc_publish_duration_seconds',
    'Длительность передачи события продюсеру Kafka',
    ['topic'],
    # Границы подобраны под ожидаемый профиль: отправка в буфер продюсера
    # занимает доли миллисекунды, всё что дольше 100 мс — уже аномалия.
    buckets=(0.0005, 0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
    registry=registry,
)

delivery_failures = Counter(
    'ugc_delivery_failures_total',
    'Число неудачных подтверждений доставки от брокера',
    ['topic'],
    registry=registry,
)

records_rejected = Counter(
    'ugc_records_rejected_total',
    'Число записей, отвергнутых по причине уровня записи (размер, топик), а не отказа кластера',
    ['topic', 'error'],
    registry=registry,
)

broker_up = Gauge(
    'ugc_broker_connected_workers',
    'Число живых воркеров с подключённым продюсером Kafka',
    registry=registry,
    # Сумма по живым процессам, а не min/max. Причина в устройстве
    # multiprocess-режима: мастер-процесс uvicorn тоже импортирует модуль и
    # заводит gauge со значением 0, но lifespan в нём не выполняется и
    # продюсера у него нет. При 'livemin' его ноль навсегда обнулял бы метрику,
    # при 'livemax' — маскировал бы отказ отдельных воркеров. Сумма читается
    # однозначно: N — все воркеры подключены, 0 — брокер недоступен целиком,
    # промежуточное значение — частичная деградация.
    multiprocess_mode='livesum',
)

fallback_buffer_size = Gauge(
    'ugc_fallback_buffer_size',
    'Текущее число записей в Redis-буфере деградации',
    registry=registry,
    # Буфер один на всех (он в Redis), поэтому берём последнее значение,
    # а не сумму по воркерам — иначе размер множился бы на число воркеров.
    multiprocess_mode='mostrecent',
)

fallback_drained = Counter(
    'ugc_fallback_drained_total',
    'Число записей, доставленных в Kafka фоновым дренажом',
    registry=registry,
)

fallback_dead_lettered = Counter(
    'ugc_fallback_dead_lettered_total',
    'Число записей, отправленных в DLQ после исчерпания попыток',
    registry=registry,
)


def build_scrape_registry() -> CollectorRegistry:
    """Возвращает реестр, из которого нужно отдавать ответ на ``/metrics``.

    Сервис работает в несколько uvicorn-воркеров, и каждый ведёт собственные
    счётчики в своём процессе. Если ``PROMETHEUS_MULTIPROC_DIR`` задан (это
    делает Dockerfile), prometheus_client пишет значения в mmap-файлы, и здесь
    они собираются в один сводный реестр — иначе скрейп попадал бы в случайный
    воркер и показывал примерно 1/N реального трафика.
    """
    if not os.environ.get('PROMETHEUS_MULTIPROC_DIR'):
        return registry
    scrape_registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(scrape_registry)
    return scrape_registry
