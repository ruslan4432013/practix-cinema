"""Реакция на ребаланс группы.

Документация aiokafka предупреждает прямо: при ручном коммите без
``ConsumerRebalanceListener`` после ребаланса прилетает ``CommitFailedError`` —
партиции уже у другого консьюмера, и коммитить их поздно. Поэтому пачка
закрывается и коммитится ДО отдачи партиций.

Ограничение по времени здесь жёсткое: ребаланс ждёт завершения колбэка, и
затянувшийся флаш выкинет нас из группы, спровоцировав следующий ребаланс —
классический ребаланс-шторм. Поэтому если ClickHouse недоступен, буфер просто
ВЫБРАСЫВАЕТСЯ без коммита. Это безопасно: непрокоммиченные события перечитает
тот, кому достанутся партиции, а дубликаты погасит ReplacingMergeTree по
event_id.
"""

import logging

from aiokafka import ConsumerRebalanceListener

from practix_etl_clickhouse.core import metrics

logger = logging.getLogger(__name__)


class FlushOnRevokeListener(ConsumerRebalanceListener):
    def __init__(self, runner) -> None:
        self._runner = runner

    async def on_partitions_revoked(self, revoked) -> None:
        metrics.rebalances.labels(phase='revoked').inc()
        logger.info('Partitions revoked', extra={'partitions': [str(tp) for tp in revoked]})
        await self._runner.on_revoke()

    async def on_partitions_assigned(self, assigned) -> None:
        metrics.rebalances.labels(phase='assigned').inc()
        logger.info('Partitions assigned', extra={'partitions': [str(tp) for tp in assigned]})
        await self._runner.on_assign(assigned)
