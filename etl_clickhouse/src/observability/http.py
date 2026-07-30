"""Экспозиция метрик.

``prometheus_client.start_http_server`` поднимает крошечный WSGI-сервер в
отдельном потоке. Для сервиса без HTTP-API это правильный размен: не тащим
FastAPI/uvicorn ради одной ручки и, что важнее, не даём скрейпу конкурировать
за event loop со вставками в ClickHouse — скрейп Prometheus приходит раз в
15 секунд и не должен ждать, пока завершится минутный ретрай вставки.

Тот же порт 8000, что у остальных сервисов стека, — чтобы конфигурация
Prometheus не отличалась от сервиса к сервису.
"""

import logging

from prometheus_client import start_http_server

from core import metrics
from core.config import settings

logger = logging.getLogger(__name__)


def start_metrics_server() -> None:
    if not settings.ETL_METRICS_ENABLED:
        logger.info('Metrics endpoint is disabled by configuration')
        return
    start_http_server(settings.ETL_METRICS_PORT, registry=metrics.registry)
    logger.info('Metrics endpoint is listening on :%d/metrics', settings.ETL_METRICS_PORT)
