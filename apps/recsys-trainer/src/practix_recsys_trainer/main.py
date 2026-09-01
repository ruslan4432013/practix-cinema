"""Долгоживущий процесс обучения по расписанию.

Не одноразовый скрипт и не cron снаружи. Причины две.

Первая — метрики. F0.4 требует, чтобы возраст витрины был ВИДЕН: устаревшая
модель должна быть инцидентом, а не тишиной. Одноразовый процесс успевает
экспортировать метрики ровно на время своей работы, то есть Prometheus не
застаёт его почти никогда, и «обучение не запускалось третьи сутки» выглядит
точно так же, как «всё хорошо».

Вторая — идемпотентность даётся схемой, а не расписанием. ``run_key`` привязан
к часу запуска, поэтому лишний тик после перезапуска контейнера ничего не
ломает: он упрётся в уникальное ограничение и завершится как «пропущено».
Именно это делает безопасным ``RECS_TRAINER_RUN_ON_START``, без которого стенд
после подъёма ждал бы сутки, прежде чем показать хоть одну рекомендацию.

Масштабировать этот процесс репликами НЕЛЬЗЯ (раздел 5.3 ТЗ): витрину пишет
ровно один писатель. Формально его защищает ``pg_advisory_lock``, но две
реплики всё равно означали бы две попытки построить одно и то же, из которых
вторая ждёт первую впустую.
"""

import logging
import time

from prometheus_client import Counter, Gauge, Histogram, start_http_server

from practix_core.sentry import init_sentry_from_env
from practix_core.tracing import init_tracer_provider
from practix_recsys_trainer.core.config import settings
from practix_recsys_trainer.core.observability import configure_logging
from practix_recsys_trainer.pipeline import run_training
from practix_recsys_trainer.shelf import writer

LOGGING = configure_logging()
logger = logging.getLogger(__name__)

# Реестр здесь глобальный, а не собственный, как в API: процесс один, воркеров
# нет, и multiproc-режим (который отключил бы ProcessCollector) не нужен —
# та же причина, по которой его нет в etl-clickhouse.
train_runs = Counter('recs_train_runs_total', 'Прогоны обучения по исходу', ['outcome'])
train_duration = Histogram(
    'recs_train_duration_seconds',
    'Длительность прогона обучения',
    # Верхняя граница выбрана по SLO: батч обязан укладываться в 2 часа, чтобы
    # помещаться в ночное окно с запасом на повтор при падении.
    buckets=(1, 5, 15, 60, 300, 900, 1800, 3600, 7200),
)
shelf_age = Gauge('recs_shelf_age_seconds', 'Возраст актуальной версии витрины')
shelf_version = Gauge('recs_shelf_version', 'Номер актуальной версии витрины')
shelf_rows = Gauge('recs_shelf_rows', 'Строк в витрине по видам списков', ['kind'])
catalog_coverage = Gauge('recs_catalog_coverage', 'Доля каталога с непустым блоком похожих')


def _refresh_shelf_metrics() -> None:
    """Публикует состояние ВИТРИНЫ, а не последнего прогона этого процесса.

    Возраст обновляется и между прогонами — иначе метрика молчала бы сутки, и
    «обучение не запускалось третьи сутки» выглядело бы точно так же, как «всё
    хорошо» (F0.4). Размеры и покрытие берутся из статистики самой версии по
    той же причине: на стенде обучение часто запускают отдельной командой, и
    гейджи, заполняемые только собственным прогоном, показывали бы ноль при
    полной витрине.
    """
    engine = writer.make_engine()
    try:
        with engine.connect() as conn:
            version, finished_at, stats = writer.read_state(conn)
    except Exception as exc:  # noqa: BLE001 — недоступность витрины не должна ронять цикл
        logger.warning('SHELF STATE UNAVAILABLE: %s', exc)
        return
    finally:
        engine.dispose()

    if version is None or finished_at is None:
        return
    shelf_version.set(version)
    shelf_age.set(max(0.0, time.time() - finished_at.timestamp()))
    catalog_coverage.set(float(stats.get('catalog_coverage', 0.0)))
    for kind, count in (stats.get('rows') or {}).items():
        shelf_rows.labels(kind=kind).set(count)


def _run_once() -> None:
    with train_duration.time():
        result = run_training()

    if result.skipped:
        train_runs.labels(outcome='skipped').inc()
        return

    train_runs.labels(outcome='success').inc()
    # Гейджи витрины заполняет _refresh_shelf_metrics — из самой витрины, а не
    # из результата этого прогона. Два источника у одной метрики означали бы,
    # что её значение зависит от того, кто обновил её последним.


def main() -> None:
    init_tracer_provider(
        enabled=settings.OTEL_ENABLED,
        service_name=settings.OTEL_SERVICE_NAME,
        endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT,
    )
    init_sentry_from_env(service_name=settings.OTEL_SERVICE_NAME)

    if settings.RECS_TRAINER_METRICS_ENABLED:
        start_http_server(settings.RECS_TRAINER_METRICS_PORT)
        logger.info('Метрики на :%s/metrics', settings.RECS_TRAINER_METRICS_PORT)

    next_run = 0.0 if settings.RECS_TRAINER_RUN_ON_START else time.monotonic() + settings.RECS_TRAINER_INTERVAL_SECONDS

    while True:
        _refresh_shelf_metrics()

        if time.monotonic() >= next_run:
            try:
                _run_once()
            except Exception:
                train_runs.labels(outcome='failure').inc()
                logger.exception('TRAINING FAILED: витрина осталась вчерашней, следующая попытка по расписанию')
            # Следующий запуск отсчитывается от КОНЦА прогона, а не от начала:
            # иначе двухчасовой батч при суточном интервале постепенно съезжал
            # бы к утру, а при интервале короче своей длительности запускался
            # бы вплотную сам за собой.
            next_run = time.monotonic() + settings.RECS_TRAINER_INTERVAL_SECONDS
            _refresh_shelf_metrics()

        # Тик короче интервала: он же обновляет возраст витрины, а метрика,
        # обновляемая раз в сутки, показывала бы вчерашнее число весь день.
        time.sleep(min(30.0, settings.RECS_TRAINER_INTERVAL_SECONDS))


if __name__ == '__main__':
    main()
