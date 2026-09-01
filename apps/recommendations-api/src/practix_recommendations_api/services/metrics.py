"""Метрики Prometheus выдачи.

Набор подобран под эксплуатационные вопросы, ради которых сервис мониторят:

* «Какая доля ответов — деградация?» — ``recs_requests_total`` размечен парой
  (запрошенный вид, отданный источник). Без этой пары подмена персональной
  выдачи популярной неотличима от здоровья: код ответа в обоих случаях 200.
* «Жив ли горячий слой?» — ``recs_cache_hits_total`` против
  ``recs_cache_misses_total`` и ``recs_cache_unavailable_total``.
* «Не устарела ли модель?» — ``recs_shelf_age_seconds`` (F0.4) и
  ``recs_shelf_version``.
* «Укладываемся ли в SLO?» — гистограмма ``recs_response_duration_seconds``.

Все метрики в отдельном реестре: так в него не попадают дефолтные коллекторы,
а тесты могут собрать значения независимо от глобального состояния.
"""

import os

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, multiprocess

registry = CollectorRegistry()

requests_total = Counter(
    'recs_requests_total',
    'Запросы выдачи: что просили и что реально отдали',
    ['kind', 'source'],
    registry=registry,
)

cache_hits = Counter(
    'recs_cache_hits_total',
    'Попадания в горячий слой Redis',
    ['kind'],
    registry=registry,
)

cache_misses = Counter(
    'recs_cache_misses_total',
    'Промахи горячего слоя: ключа нет, читаем витрину из PostgreSQL',
    ['kind'],
    registry=registry,
)

cache_unavailable = Counter(
    'recs_cache_unavailable_total',
    'Отказы горячего слоя: Redis недоступен, чтение ушло в PostgreSQL',
    ['operation'],
    registry=registry,
)

shelf_unavailable = Counter(
    'recs_shelf_unavailable_total',
    'Отказы витрины: PostgreSQL недоступен, отдаём последнее известное популярное',
    registry=registry,
)

enrich_failures = Counter(
    'recs_enrich_failures_total',
    'Обогащение карточек из Movies API не удалось; отдаём голые идентификаторы',
    ['reason'],
    registry=registry,
)

response_duration = Histogram(
    'recs_response_duration_seconds',
    'Длительность сборки ответа выдачи',
    ['kind'],
    # Границы поставлены вокруг SLO: p95 < 200 мс, p99 < 300 мс (раздел 5.1 ТЗ).
    # Без бакетов рядом с порогом гистограмма показала бы «всё быстрее секунды»
    # и на вопрос про SLO не ответила бы.
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 1.0),
    registry=registry,
)

shelf_age = Gauge(
    'recs_shelf_age_seconds',
    'Возраст актуальной версии витрины в секундах (F0.4: устаревшая модель — инцидент)',
    registry=registry,
    # Витрина одна на всех воркеров, поэтому берём последнее значение, а не
    # сумму: сумма множила бы возраст на число процессов.
    multiprocess_mode='mostrecent',
)

shelf_version = Gauge(
    'recs_shelf_version',
    'Номер версии витрины, из которой сейчас отдаётся выдача',
    registry=registry,
    multiprocess_mode='mostrecent',
)


def build_scrape_registry() -> CollectorRegistry:
    """Реестр, из которого отдаётся ответ на ``/metrics``.

    Сервис работает в четыре воркера uvicorn, и каждый ведёт собственные
    счётчики в своём процессе. Если ``PROMETHEUS_MULTIPROC_DIR`` задан (это
    делает Dockerfile), prometheus_client пишет значения в mmap-файлы, и здесь
    они собираются в один сводный реестр — иначе скрейп попадал бы в случайный
    воркер и показывал примерно четверть реального трафика.
    """
    if not os.environ.get('PROMETHEUS_MULTIPROC_DIR'):
        return registry
    scrape_registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(scrape_registry)
    return scrape_registry
