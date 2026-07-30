"""Повтор операции с экспоненциальной задержкой — синхронный и асинхронный.

До вынесения в библиотеку в репозитории было ПЯТЬ реализаций повтора с ЧЕТЫРЬМЯ
разными кривыми задержки: два почти идентичных декоратора (`etl/lib/backoff.py`
и `tests/functional/utils/retry_utils.py`), асинхронный порт с джиттером
(`etl_clickhouse/src/core/backoff.py`) и два самодельных цикла в
`rest/services/auth_client.py` (линейный!) и `django_admin/.../auth_backend.py`.

Дублировалась именно АРИФМЕТИКА — а она расходится молча: разницу между
`start * factor**n` и `delay *= 2` в код-ревью не видно, и заметна она только
как «почему-то этот сервис долбит хранилище чаще остальных».

О ЧИСЛЕ ПОПЫТОК. ``max_attempts=None`` означает «повторять бесконечно» и это
сохранённое поведение `etl/lib/backoff.py`: фоновому конвейеру Postgres ->
Elasticsearch правильно пережить ночной отказ базы, а не превратиться в
crash-loop. Ограничение НЕ добавлено по умолчанию сознательно; вместо этого
бесконечность стала явным аргументом на стороне вызова, а не свойством
отдельного форка файла.
"""

import asyncio
import logging
import random
import time
from collections.abc import Callable, Sequence
from functools import wraps

logger = logging.getLogger(__name__)


def compute_delay(
    attempt: int,
    *,
    start: float = 0.1,
    factor: float = 2,
    border: float = 10,
    jitter: float = 0.0,
) -> float:
    """t = start * factor**attempt, ограниченное border, плюс джиттер +-jitter*t.

    ``jitter=0.0`` по умолчанию: у синхронных потребителей джиттера не было, и
    добавлять его «в подарок» при извлечении значило бы менять поведение под
    видом рефакторинга. ETL ClickHouse передаёт 0.2 явно — там джиттер нужен,
    чтобы несколько инстансов, поймавших один отказ, не ломились в хранилище
    синхронно (thundering herd).
    """
    delay = min(start * (factor**attempt), border)
    if jitter:
        spread = delay * jitter
        delay = max(0.0, delay + random.uniform(-spread, spread))
    return delay


async def backoff_sleep(
    attempt: int,
    *,
    start: float = 0.1,
    factor: float = 2,
    border: float = 10,
    jitter: float = 0.0,
) -> float:
    """Спит вычисленное время и возвращает его (для логов и метрик).

    Асинхронная форма обязательна для консьюмеров Kafka: ``time.sleep``
    заблокировал бы event loop вместе с хартбитами, и группа исключила бы
    консьюмера ровно во время аварии, которую он пережидает.
    """
    delay = compute_delay(attempt, start=start, factor=factor, border=border, jitter=jitter)
    await asyncio.sleep(delay)
    return delay


def retry(
    start_sleep_time: float = 0.1,
    factor: float = 2,
    border_sleep_time: float = 10,
    max_attempts: int | None = None,
    exceptions: Sequence[type[BaseException]] | tuple[type[BaseException], ...] = (Exception,),
) -> Callable:
    """Декоратор: повторяет синхронный вызов при ошибке, увеличивая паузу.

    :param max_attempts: сколько всего попыток сделать. ``None`` — бесконечно
        (поведение фонового ETL); число — после его исчерпания последнее
        исключение пробрасывается наружу.
    """
    exceptions = tuple(exceptions)

    def func_wrapper(func):
        @wraps(func)
        def inner(*args, **kwargs):
            n = 0
            attempt = 0
            while True:
                attempt += 1
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    if max_attempts is not None and attempt >= max_attempts:
                        logger.error(
                            'Функция %s не выполнилась за %d попыток, сдаёмся',
                            func.__name__,
                            max_attempts,
                        )
                        raise
                    sleep_time = compute_delay(n, start=start_sleep_time, factor=factor, border=border_sleep_time)
                    if sleep_time < border_sleep_time:
                        n += 1
                    if max_attempts is None:
                        logger.warning(
                            'Ошибка в %s: %s. Повтор через %.2f сек.',
                            func.__name__,
                            exc,
                            sleep_time,
                        )
                    else:
                        logger.warning(
                            'Ошибка в %s (%d/%d): %s. Повтор через %.2f сек.',
                            func.__name__,
                            attempt,
                            max_attempts,
                            exc,
                            sleep_time,
                        )
                    time.sleep(sleep_time)

        return inner

    return func_wrapper
