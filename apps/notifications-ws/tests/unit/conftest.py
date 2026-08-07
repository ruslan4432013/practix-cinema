"""Заглушки внешнего мира для юнит-набора шлюза.

Свой ``FakeRedis``, а не ``fakeredis`` из PyPI: нужны ровно три команды
(``set``/``getdel``/``get``) и возможность заставить их падать. Зависимость ради
этого протащила бы в образ ещё один пакет, а «падает по требованию» она всё
равно не умеет. Тот же приём уже применён в
``apps/notifications/tests/unit/test_jwt_auth.py``.
"""

import pytest

from practix_notifications_ws.services.hub import ConnectionHub


class FakeRedis:
    """Минимальный Redis: значения без TTL, ошибки по требованию."""

    def __init__(self, *, explode: Exception | None = None) -> None:
        self.data: dict[str, str] = {}
        self.explode = explode

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._maybe_explode()
        self.data[key] = value

    async def getdel(self, key: str) -> str | None:
        self._maybe_explode()
        return self.data.pop(key, None)

    async def get(self, key: str) -> str | None:
        self._maybe_explode()
        return self.data.get(key)

    def _maybe_explode(self) -> None:
        if self.explode is not None:
            raise self.explode


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def hub() -> ConnectionHub:
    """Маленькие лимиты: переполнение и отказ должны достигаться в тесте, а не
    имитироваться подменой констант."""
    return ConnectionHub(queue_size=3, max_per_user=2, max_total=4, max_pollers_per_user=3, max_pollers=5)
