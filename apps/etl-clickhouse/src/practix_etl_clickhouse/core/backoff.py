"""Асинхронная экспоненциальная пауза между попытками.

Формула переехала в ``practix_core.backoff`` (она дублировалась в пяти местах);
здесь остались только значения, специфичные для этого сервиса.

Джиттер обязателен именно здесь: несколько инстансов ETL, синхронно поймавших
один и тот же отказ ClickHouse, иначе ломятся в него одновременно и на каждой
итерации — тот самый thundering herd, который мешает хранилищу подняться.
В общей библиотеке джиттер по умолчанию выключен, чтобы у синхронных
потребителей поведение при извлечении не изменилось, поэтому он передаётся явно.

Синхронный ``practix_core.backoff.retry`` здесь не годится: ``time.sleep``
заблокировал бы event loop вместе с хартбитами консьюмера, и группа исключила бы
ETL ровно во время аварии, которую он пережидает.
"""

from practix_core.backoff import backoff_sleep as _backoff_sleep
from practix_core.backoff import compute_delay as _compute_delay

# Разброс задержки +-20 %.
_JITTER_RATIO = 0.2


def compute_delay(attempt: int, start: float, factor: float, border: float) -> float:
    """t = start * factor**attempt, ограниченное border, плюс джиттер +-20 %."""
    return _compute_delay(attempt, start=start, factor=factor, border=border, jitter=_JITTER_RATIO)


async def backoff_sleep(attempt: int, start: float, factor: float, border: float) -> float:
    """Спит вычисленное время и возвращает его (для логов и метрик)."""
    return await _backoff_sleep(attempt, start=start, factor=factor, border=border, jitter=_JITTER_RATIO)
