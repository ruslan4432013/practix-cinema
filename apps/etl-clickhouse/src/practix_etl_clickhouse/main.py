"""Точка входа ETL Kafka -> ClickHouse.

Порядок запуска не произволен:

1. логирование — чтобы всё последующее было видно;
2. ``MemoryWatcher.start()`` — tracemalloc обязан включиться ДО создания
   консьюмера и клиента, иначе аллокации старта не попадут в базовую линию и
   будут выглядеть отсутствующими, а не постоянными;
3. HTTP-эндпоинт метрик — поднимается раньше подключений, чтобы Prometheus
   видел сервис даже когда тот ждёт лежащую Kafka;
4. приёмник и источник.
"""

import asyncio
import contextlib
import logging
import signal

from practix_core.sentry import init_sentry
from practix_etl_clickhouse.brokers.consumer import build_consumer, start_with_retry
from practix_etl_clickhouse.brokers.rebalance import FlushOnRevokeListener
from practix_etl_clickhouse.core import metrics
from practix_etl_clickhouse.core.config import settings
from practix_etl_clickhouse.core.logger import setup_logging
from practix_etl_clickhouse.core.memory import MemoryWatcher
from practix_etl_clickhouse.core.tracing import init_tracer_provider
from practix_etl_clickhouse.observability.http import start_metrics_server
from practix_etl_clickhouse.pipeline.buffer import BatchBuffer
from practix_etl_clickhouse.pipeline.reporters import report_lag, report_sink_health
from practix_etl_clickhouse.pipeline.runner import Runner
from practix_etl_clickhouse.sinks.base import SinkUnavailableError
from practix_etl_clickhouse.sinks.clickhouse import ClickHouseSink

setup_logging()
logger = logging.getLogger('etl.main')


async def _connect_sink(sink: ClickHouseSink, stop_event: asyncio.Event) -> bool:
    """Ждёт ClickHouse так же, как консьюмер ждёт Kafka.

    Падать на старте нельзя по той же причине: перезапуск контейнера не
    поднимет хранилище, а превратит его недоступность в рестарт-луп.
    """
    while not stop_event.is_set():
        try:
            await sink.start()
            metrics.clickhouse_up.set(1)
            return True
        except SinkUnavailableError as exc:
            metrics.clickhouse_up.set(0)
            logger.warning(
                'ClickHouse is not available yet, retrying',
                extra={'error': str(exc), 'retry_in': settings.CH_HEALTH_INTERVAL},
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=settings.CH_HEALTH_INTERVAL)
            except TimeoutError:
                continue
    return False


async def run() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    memory_watcher = MemoryWatcher()
    memory_watcher.start()
    # Трассировка поднимается ДО консьюмера: AIOKafkaInstrumentor подменяет
    # методы класса, и консьюмер, созданный раньше, инструментации не получит.
    init_tracer_provider()
    # Сбор ошибок. У фонового консьюмера нет ни HTTP-ответа, ни пользователя,
    # который пожалуется: до сих пор единственным следом аварии была строка
    # logger.exception в stdout. LoggingIntegration превращает её в событие.
    init_sentry(
        enabled=settings.SENTRY_ENABLED,
        dsn=settings.SENTRY_DSN,
        service_name=settings.OTEL_SERVICE_NAME,
        environment=settings.SENTRY_ENVIRONMENT,
        release=settings.SENTRY_RELEASE,
        sample_rate=settings.SENTRY_SAMPLE_RATE,
        send_default_pii=settings.SENTRY_SEND_DEFAULT_PII,
    )
    start_metrics_server()

    sink = ClickHouseSink()
    buffer = BatchBuffer()

    # Ссылки на фоновые задачи удерживаются в множестве: без сильной ссылки
    # сборщик мусора вправе уничтожить задачу прямо во время работы
    # (тот же приём, что в analytics_collector/src/brokers/kafka.py).
    background: set[asyncio.Task] = set()

    def spawn(coro, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        background.add(task)
        task.add_done_callback(background.discard)

    spawn(memory_watcher.run(stop_event), 'memory-watcher')

    if not await _connect_sink(sink, stop_event):
        return
    spawn(report_sink_health(sink, stop_event), 'sink-health')

    runner_holder: dict = {}
    listener = FlushOnRevokeListener(_LazyRunner(runner_holder))
    consumer = build_consumer(listener)
    runner = Runner(consumer, sink, buffer, stop_event)
    runner_holder['runner'] = runner

    if not await start_with_retry(consumer, stop_event):
        await sink.close()
        return
    spawn(report_lag(consumer, stop_event), 'lag-reporter')

    logger.info('ETL started', extra={'group_id': settings.ETL_CONSUMER_GROUP, 'topics': settings.topics})
    try:
        await runner.run()
    finally:
        # Порядок остановки обратен порядку запуска. consumer.stop() делает
        # штатный выход из группы: партиции сразу переходят к другому
        # инстансу, а не ждут session_timeout_ms.
        stop_event.set()
        for task in list(background):
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await consumer.stop()
        await sink.close()
        memory_watcher.stop()
        metrics.kafka_up.set(0)
        metrics.clickhouse_up.set(0)
        logger.info('ETL stopped')


class _LazyRunner:
    """Отложенная ссылка на Runner.

    Listener нужен консьюмеру в момент subscribe(), а Runner — только после
    создания консьюмера. Прокси разрывает эту циклическую зависимость, не
    заводя глобальную переменную.
    """

    def __init__(self, holder: dict) -> None:
        self._holder = holder

    async def on_revoke(self) -> None:
        runner = self._holder.get('runner')
        if runner is not None:
            await runner.on_revoke()

    async def on_assign(self, assigned) -> None:
        runner = self._holder.get('runner')
        if runner is not None:
            await runner.on_assign(assigned)


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == '__main__':
    main()
