"""Повтор операции с экспоненциальной задержкой.

ДВА ОТЛИЧИЯ ОТ ПРЕЖНЕГО ``utils/backoff.py``, из-за которых модуль и переименован.

1. **Имя.** Скрипты ожидания делали ``from backoff import backoff``, и это
   работало только потому, что каталог скрипта попадает в ``sys.path`` при
   запуске файлом. Стоит появиться в окружении одноимённому пакету с PyPI
   (``backoff`` — популярная библиотека, её легко притащить транзитивно), и
   импорт разрешится в него: ``backoff.backoff`` там тоже есть, но с другой
   сигнатурой. Разбираться в такой поломке долго, а имя ``retry_utils`` с PyPI
   не конфликтует.

2. **Ограничение числа попыток.** Прежняя версия повторяла БЕСКОНЕЧНО: при
   недоступном сервисе тест-раннер висел вечно вместо внятного падения по
   таймауту, и в CI это выглядело бы как зависший job, а не как красный тест.
   Теперь есть ``max_attempts``, после которого исключение пробрасывается
   наружу (так же устроен ``wait_for_kafka.py``).
"""

import logging
import time
from functools import wraps

logger = logging.getLogger(__name__)


def backoff(
    start_sleep_time: float = 0.1,
    factor: int = 2,
    border_sleep_time: float = 10,
    max_attempts: int = 30,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
):
    """Повторяет вызов функции при ошибке, увеличивая паузу.

    Формула задержки::

        t = start_sleep_time * (factor ^ n), пока t < border_sleep_time
        t = border_sleep_time, дальше

    :param start_sleep_time: начальная пауза
    :param factor: во сколько раз растёт пауза на каждой итерации
    :param border_sleep_time: потолок паузы
    :param max_attempts: сколько всего попыток сделать, прежде чем сдаться
    :param exceptions: какие исключения считать поводом для повтора
    :raises: последнее пойманное исключение, если попытки исчерпаны
    """

    def func_wrapper(func):
        @wraps(func)
        def inner(*args, **kwargs):
            n = 0
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    if attempt >= max_attempts:
                        logger.error(
                            'Функция %s не выполнилась за %d попыток, сдаёмся',
                            func.__name__,
                            max_attempts,
                        )
                        raise
                    sleep_time = start_sleep_time * (factor**n)
                    if sleep_time >= border_sleep_time:
                        sleep_time = border_sleep_time
                    else:
                        n += 1
                    logger.warning(
                        'Ошибка в %s (%d/%d): %s. Повтор через %.2f сек.',
                        func.__name__,
                        attempt,
                        max_attempts,
                        exc,
                        sleep_time,
                    )
                    time.sleep(sleep_time)
            # Недостижимо: последняя попытка либо возвращает, либо пробрасывает.
            raise RuntimeError('unreachable')

        return inner

    return func_wrapper
