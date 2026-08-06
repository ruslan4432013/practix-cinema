"""Общие на процесс объекты: реестр соединений и консьюмер брокера.

Модульные синглтоны, а не ``app.state``: их читают и роутеры, и фоновая задача
консьюмера, у которой объекта приложения нет. Тот же приём, что в
``analytics_collector/services/providers.py``.

Собираются в ``lifespan`` — на импорте здесь не создаётся ничего, чтобы импорт
модуля в юнит-тесте не требовал ни event loop, ни настроек.
"""

from practix_notifications_ws.brokers.consumer import PushConsumer
from practix_notifications_ws.services.hub import ConnectionHub

hub: ConnectionHub | None = None
consumer: PushConsumer | None = None


def get_hub() -> ConnectionHub:
    if hub is None:  # pragma: no cover — означало бы запрос до старта lifespan
        raise RuntimeError('ConnectionHub is not initialised')
    return hub


def get_consumer() -> PushConsumer | None:
    return consumer
