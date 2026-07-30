"""Повтор операции с экспоненциальной задержкой — обёртка над общей библиотекой.

ПОЧЕМУ МОДУЛЬ НЕ НАЗЫВАЕТСЯ ``backoff``. Скрипты ожидания делали
``from backoff import backoff``, и это работало только потому, что каталог
скрипта попадает в ``sys.path`` при запуске файлом. Стоит появиться в окружении
одноимённому пакету с PyPI (``backoff`` — популярная библиотека, её легко
притащить транзитивно), и импорт разрешится в него: ``backoff.backoff`` там тоже
есть, но с другой сигнатурой. Имя ``retry_utils`` с PyPI не конфликтует.

ПОЧЕМУ ЗДЕСЬ ЕСТЬ ``max_attempts``, А В ``etl/lib/backoff.py`` НЕТ. Тестам нужна
ровно противоположная политика: при недоступном сервисе набор должен упасть
внятной ошибкой, а не висеть вечно — в CI бесконечный повтор выглядел бы как
зависший job, а не как красный тест. Продовому конвейеру, наоборот, правильно
пережидать отказ базы. Арифметика у них теперь общая
(``practix_core.backoff``), политика — по-прежнему разная, и это единственное,
что осталось в этих двух файлах.
"""

from practix_core.backoff import retry

_DEFAULT_MAX_ATTEMPTS = 30


def backoff(
    start_sleep_time: float = 0.1,
    factor: int = 2,
    border_sleep_time: float = 10,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
):
    """Повторяет вызов функции при ошибке; после ``max_attempts`` пробрасывает его.

    Формула задержки::

        t = start_sleep_time * (factor ^ n), пока t < border_sleep_time
        t = border_sleep_time, дальше
    """
    return retry(
        start_sleep_time=start_sleep_time,
        factor=factor,
        border_sleep_time=border_sleep_time,
        max_attempts=max_attempts,
        exceptions=exceptions,
    )
