"""Повтор с экспоненциальной задержкой — тонкая обёртка над общей библиотекой.

Арифметика задержки переехала в ``practix_core.backoff``: она дублировалась в
пяти местах с четырьмя разными кривыми, и расхождение таких формул в код-ревью
не видно. Здесь остался только выбор политики повторов, специфичный для этого
конвейера.

``max_attempts=None`` — БЕСКОНЕЧНЫЕ повторы, и это осознанное решение, а не
недосмотр: фоновый конвейер Postgres -> Elasticsearch должен пережить ночной
отказ базы и продолжить, а не упасть в crash-loop. Прежде бесконечность была
неявным свойством форкнутого файла; теперь это явный аргумент в одной строке.
Ограниченный вариант нужен тестам — см. ``practix_testing``/``retry_utils``.
"""

from practix_core.backoff import retry


def backoff(
    start_sleep_time: float = 0.1,
    factor: int = 2,
    border_sleep_time: float = 10,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
):
    """Повторяет вызов функции при ошибке, увеличивая паузу. Не сдаётся никогда.

    Сигнатура сохранена для совместимости с существующими вызовами в ``gears/``.
    """
    return retry(
        start_sleep_time=start_sleep_time,
        factor=factor,
        border_sleep_time=border_sleep_time,
        max_attempts=None,
        exceptions=exceptions,
    )
