# Контекст сборки — корень репозитория (как у auth/Dockerfile):
#   docker build -f analytics_collector/Dockerfile .
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Пакеты сервиса импортируются от корня src (from core.config import ...),
    # как в auth/ (PYTHONPATH=/opt/app/auth/src).
    PYTHONPATH=/opt/app/analytics_collector/src \
    # Каталог для метрик из нескольких процессов uvicorn: без него скрейп
    # /metrics попадал бы в случайный воркер (см. core/metrics.py).
    PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus_multiproc

WORKDIR /opt/app/analytics_collector

COPY analytics_collector/requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY analytics_collector/ .
RUN chmod +x entrypoint.sh

# Сервис не должен работать от root: ingest-ручка публичная, и любая уязвимость
# в разборе запроса не должна давать привилегий в контейнере. Каталоги, в
# которые процесс пишет, передаются во владение appuser.
RUN groupadd --system --gid 1001 appuser \
    && useradd --system --uid 1001 --gid appuser appuser \
    && mkdir -p "$PROMETHEUS_MULTIPROC_DIR" \
    && chown -R appuser:appuser /opt/app/analytics_collector "$PROMETHEUS_MULTIPROC_DIR"
USER appuser

EXPOSE 8000

# Проба живости не зависит от состояния Kafka: перезапуск контейнера не чинит
# упавший брокер, а лишь превратил бы деградацию в отказ (см. api/v1/health.py).
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2).status == 200 else 1)"

ENTRYPOINT ["/opt/app/analytics_collector/entrypoint.sh"]

# Четыре воркера: ingest — это I/O-bound работа, и несколько процессов дают
# запас по CPU на сериализацию JSON и сжатие батчей продюсером.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "src", "--workers", "4"]
