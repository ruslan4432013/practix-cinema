# Контекст сборки — корень репозитория (как у analytics_collector/Dockerfile):
#   docker build -f etl_clickhouse/Dockerfile .
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Пакеты сервиса импортируются от корня src (from core.config import ...),
    # как в analytics_collector/ и auth/.
    PYTHONPATH=/opt/app/etl_clickhouse/src
# PROMETHEUS_MULTIPROC_DIR намеренно НЕ задан: сервис однопроцессный, а
# multiproc-режим ломает ProcessCollector и GCCollector — ровно те коллекторы,
# ради которых метрики памяти и вводятся (см. src/core/metrics.py).

WORKDIR /opt/app/etl_clickhouse

COPY etl_clickhouse/requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY etl_clickhouse/ .
RUN chmod +x entrypoint.sh

# Сервис не должен работать от root: он разбирает данные, пришедшие из
# браузера, пусть и через Kafka.
RUN groupadd --system --gid 1001 appuser \
    && useradd --system --uid 1001 --gid appuser appuser \
    && chown -R appuser:appuser /opt/app/etl_clickhouse
USER appuser

EXPOSE 8000

# Проба живости бьёт в /metrics — единственную ручку сервиса. Она намеренно НЕ
# зависит от доступности Kafka и ClickHouse: перезапуск контейнера не чинит
# упавшее хранилище, а лишь превратил бы переживаемую деградацию (данные ждут
# в Kafka) в бесконечный рестарт-луп с потерей накопленной пачки.
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/metrics', timeout=2).status == 200 else 1)"

ENTRYPOINT ["/opt/app/etl_clickhouse/entrypoint.sh"]

# Один процесс, никаких воркеров: параллелизм чтения даётся партициями Kafka и
# горизонтальным масштабированием контейнеров (каждый — отдельный участник
# одной consumer group), а не потоками внутри одного процесса.
CMD ["python", "-m", "main"]
