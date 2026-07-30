"""Контракт приёмника и классификация его отказов.

Разделение отказов на два класса — не формальность, а решение о том, что
делать дальше:

* ``SinkUnavailableError`` — хранилище недоступно или перегружено. Повторять
  можно и нужно: данные ждут в Kafka, оффсеты не сдвинуты, потери нет.
* ``SinkSchemaError`` — данные не лезут в схему. Повторять бесполезно: это баг
  в DDL или в трансформации, и тысяча попыток даст тысячу одинаковых ошибок.

Оффсеты не коммитятся ни в том, ни в другом случае — отказ должен быть громким.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence


class SinkError(Exception):
    """Базовый отказ приёмника."""


class SinkUnavailableError(SinkError):
    """Временный отказ: сеть, таймаут, перегрузка, недоступная реплика."""


class SinkSchemaError(SinkError):
    """Постоянный отказ: несовместимость данных и схемы."""


class Sink(ABC):
    """Приёмник строк."""

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def insert(
        self,
        table: str,
        rows: Sequence[Sequence],
        column_names: Sequence[str],
        column_type_names: Sequence[str],
        dedup_token: str,
    ) -> int:
        """Вставляет строки, возвращает число записанных."""

    @abstractmethod
    async def ping(self) -> bool: ...

    @abstractmethod
    async def close(self) -> None: ...
