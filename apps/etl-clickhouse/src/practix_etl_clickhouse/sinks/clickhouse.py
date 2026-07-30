"""Приёмник ClickHouse.

Ретраи здесь сознательно отсутствуют: решение «повторить, встать на паузу или
отдать партиции» принимает конвейер (``pipeline/runner.py``), потому что оно
касается оффсетов и потребления памяти, а не приёмника. Задача этого модуля —
выполнить вставку и честно классифицировать отказ.
"""

import hashlib
import logging
import time
from collections.abc import Sequence
from typing import Any

import clickhouse_connect
from clickhouse_connect.driver.exceptions import (
    DatabaseError,
    InterfaceError,
    OperationalError,
    ProgrammingError,
)

from practix_etl_clickhouse.core import metrics
from practix_etl_clickhouse.core.config import settings
from practix_etl_clickhouse.sinks.base import Sink, SinkSchemaError, SinkUnavailableError

logger = logging.getLogger(__name__)


def _base_settings() -> dict[str, Any]:
    """Настройки, применяемые ко ВСЕМ вставкам.

    Каждая закрывает конкретный способ потерять или задвоить данные.
    """
    return {
        # КРИТИЧНО. По умолчанию (distributed_foreground_insert=0) вставка в
        # Distributed асинхронная: сервер кладёт данные в спул-каталог
        # координатора и отвечает «ок» ДО того, как шард их принял. ETL
        # прокоммитил бы оффсеты по такому «ок», и падение узла-координатора
        # уничтожило бы данные, которые Kafka уже считает доставленными.
        'distributed_foreground_insert': 1,
        # Та же ловушка этажом выше: async_insert подтверждает по буферу
        # сервера, а не по записи на диск.
        'async_insert': 0,
        # Материализованное представление срабатывает на вставляемый блок, а не
        # на слитые данные, поэтому повторная вставка пачки не создала бы
        # дубликат в film_views, но посчиталась бы второй раз в витринах.
        'deduplicate_blocks_in_dependent_materialized_views': 1,
        # Аналог acks=all + min.insync.replicas=2 в Kafka: ждём подтверждения
        # от обеих реплик шарда.
        'insert_quorum': settings.CH_INSERT_QUORUM,
        # При параллельном кворуме дедупликация по insert_deduplication_token
        # не гарантируется — а она у нас второй уровень защиты от дублей.
        'insert_quorum_parallel': 0,
    }


def build_dedup_token(table: str, ranges: dict[tuple[str, int], tuple[int, int]]) -> str:
    """Детерминированный токен идемпотентности пачки.

    Считается из диапазонов оффсетов: если ETL упал между вставкой и коммитом,
    после перезапуска он прочитает ровно те же оффсеты, соберёт ровно ту же
    пачку и получит ровно тот же токен — и ClickHouse отбросит повтор.

    ИНВАРИАНТ, КОТОРЫЙ ЛЕГКО СЛОМАТЬ НЕЗАМЕТНО. Всё держится на том, что
    перезапущенный процесс собирает ИМЕННО ТУ ЖЕ пачку. Любая правка батчинга —
    разбиение пачки по размеру, отдельные границы для ``raw_events`` и
    ``film_views``, изменение порядка таблиц — даст другие диапазоны, другой
    токен и, значит, дубликаты. Ошибки при этом не будет нигде: ни в логе, ни в
    метриках, ни в схеме — только тихо задвоенные данные.

    Поэтому инвариант закреплён функциональным тестом
    ``tests/functional/etl/test_idempotency.py::TestCrashBetweenInsertAndCommit``:
    первый прогон падает между вставкой и коммитом, второй перечитывает те же
    сообщения, и дублей в ClickHouse быть не должно. Соседний тест показывает
    обратную сторону — при другом токене те же строки вставляются дважды.
    """
    parts = '|'.join(f'{topic}:{partition}:{low}-{high}' for (topic, partition), (low, high) in sorted(ranges.items()))
    return f'{table}:{hashlib.blake2b(parts.encode("utf-8"), digest_size=16).hexdigest()}'


class ClickHouseSink(Sink):
    def __init__(self) -> None:
        self._client = None
        self._host_index = 0

    def _next_host(self) -> tuple[str, int]:
        """Перебирает координаторов по кругу.

        Адреса в CH_HOSTS — это по одному узлу на шард, а не реплики: вставка
        идёт в Distributed-таблицу, которая сама разносит строки. Второй адрес
        нужен, чтобы падение узла-координатора не останавливало запись
        целиком.
        """
        hosts = settings.clickhouse_hosts
        host = hosts[self._host_index % len(hosts)]
        self._host_index += 1
        return host

    async def start(self) -> None:
        host, port = self._next_host()
        try:
            self._client = await clickhouse_connect.get_async_client(
                host=host,
                port=port,
                username=settings.CH_USER,
                password=settings.CH_PASSWORD,
                database=settings.CH_DATABASE,
                secure=settings.CH_SECURE,
                connect_timeout=settings.CH_CONNECT_TIMEOUT,
                # Единственный работающий таймаут на саму вставку. Заворачивать
                # insert в asyncio.wait_for нельзя: отмена корутины не
                # отменяет уже отправленный HTTP-запрос, и сервер продолжил бы
                # вставку, о которой мы решили, что она не состоялась.
                send_receive_timeout=settings.CH_SEND_RECEIVE_TIMEOUT,
                connector_limit=settings.CH_CONNECTOR_LIMIT,
                settings=_base_settings(),
            )
        except (OperationalError, InterfaceError, OSError) as exc:
            raise SinkUnavailableError(f'cannot connect to ClickHouse at {host}:{port}: {exc}') from exc
        logger.info('ClickHouse sink connected', extra={'host': host, 'port': port})

    async def reconnect(self) -> None:
        """Пересоздаёт клиент, переключаясь на следующего координатора."""
        await self.close()
        await self.start()

    async def insert(
        self,
        table: str,
        rows: Sequence[Sequence],
        column_names: Sequence[str],
        column_type_names: Sequence[str],
        dedup_token: str,
    ) -> int:
        if not rows:
            return 0
        if self._client is None:
            raise SinkUnavailableError('ClickHouse client is not started')

        started = time.perf_counter()
        try:
            summary = await self._client.insert(
                table,
                list(rows),
                # Имена и типы передаются всегда и явно: без них драйвер
                # выводит типы по первой строке и спотыкается на
                # Nullable(UUID), где первая строка сплошь и рядом None.
                column_names=list(column_names),
                column_type_names=list(column_type_names),
                settings={'insert_deduplication_token': dedup_token},
            )
        except ProgrammingError as exc:
            raise SinkSchemaError(f'insert into {table} rejected by ClickHouse: {exc}') from exc
        except (OperationalError, InterfaceError, OSError, TimeoutError) as exc:
            raise SinkUnavailableError(f'insert into {table} failed: {exc}') from exc
        except DatabaseError as exc:
            # Остальные ошибки БД неоднозначны (нехватка памяти, слишком много
            # частей). Считаем их временными: данные ждут в Kafka, повтор
            # дешевле потери, а бесконечный цикл видно по метрикам и алертам.
            raise SinkUnavailableError(f'insert into {table} failed: {exc}') from exc
        finally:
            metrics.insert_duration.labels(table=table).observe(time.perf_counter() - started)

        written = getattr(summary, 'written_rows', len(rows)) or len(rows)
        metrics.rows_inserted.labels(table=table).inc(len(rows))
        return written

    async def ping(self) -> bool:
        """Активная проба доступности.

        Нужна именно активная: без неё метрика etl_ch_clickhouse_up показывала
        бы 1 после аварии просто потому, что мы давно ничего не вставляли
        (та же логика, что у KafkaEventBroker._probe в коллекторе).
        """
        if self._client is None:
            return False
        try:
            await self._client.command('SELECT 1')
            return True
        except Exception:  # noqa: BLE001 — проба не должна ронять фоновую задачу
            return False

    async def close(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.close()
        except Exception:  # noqa: BLE001 — закрытие битого соединения не важно
            logger.debug('Error while closing ClickHouse client', exc_info=True)
        finally:
            self._client = None
