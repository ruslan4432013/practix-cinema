import logging
import time
from functools import wraps

logger = logging.getLogger(__name__)


def backoff(
    start_sleep_time: float = 0.1,
    factor: int = 2,
    border_sleep_time: float = 10,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
):
    """
    Функция для повторного выполнения функции через некоторое время,
    если возникла ошибка. Использует наивный экспоненциальный рост времени
    повтора (factor) до граничного времени ожидания (border_sleep_time).

    Формула:
        t = start_sleep_time * (factor ^ n), если t < border_sleep_time
        t = border_sleep_time, иначе

    :param start_sleep_time: начальное время ожидания
    :param factor: во сколько раз нужно увеличивать время ожидания на каждой итерации
    :param border_sleep_time: максимальное время ожидания
    :param exceptions: какие исключения перехватывать
    :return: результат выполнения функции
    """

    def func_wrapper(func):
        @wraps(func)
        def inner(*args, **kwargs):
            n = 0
            while True:
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    sleep_time = start_sleep_time * (factor**n)
                    if sleep_time >= border_sleep_time:
                        sleep_time = border_sleep_time
                    else:
                        n += 1
                    logger.warning(
                        'Ошибка в %s: %s. Повтор через %.2f сек.',
                        func.__name__,
                        exc,
                        sleep_time,
                    )
                    time.sleep(sleep_time)

        return inner

    return func_wrapper
