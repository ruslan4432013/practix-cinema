"""Асинхронная экспоненциальная пауза между попытками.

``etl/lib/backoff.py`` (Postgres -> Elasticsearch) здесь не подходит: он
синхронный и ``time.sleep`` заблокировал бы event loop вместе с хартбитами
консьюмера, из-за чего группа исключила бы ETL ровно во время аварии,
которую он пережидает. Формула и стиль логирования сохранены.
"""

import asyncio
import logging
import random

logger = logging.getLogger(__name__)

# Разброс задержки. Без него несколько инстансов ETL, синхронно поймавших один
# и тот же отказ ClickHouse, будут ломиться в него одновременно и на каждой
# итерации — тот самый thundering herd, который мешает хранилищу подняться.
_JITTER_RATIO = 0.2


def compute_delay(attempt: int, start: float, factor: float, border: float) -> float:
    """t = start * factor**attempt, ограниченное border, плюс джиттер +-20 %."""
    delay = min(start * (factor**attempt), border)
    jitter = delay * _JITTER_RATIO
    return max(0.0, delay + random.uniform(-jitter, jitter))


async def backoff_sleep(attempt: int, start: float, factor: float, border: float) -> float:
    """Спит вычисленное время и возвращает его (для логов и метрик)."""
    delay = compute_delay(attempt, start, factor, border)
    await asyncio.sleep(delay)
    return delay
