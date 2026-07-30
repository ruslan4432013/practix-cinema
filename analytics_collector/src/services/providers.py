"""Модульные синглтоны прикладных компонентов.

Тот же приём, что в ``rest/db/redis_db.py`` и ``rest/services/auth_client.py``:
компоненты создаются в ``lifespan`` (соединения нельзя открывать до старта
event loop) и раздаются отсюда как зависимости FastAPI.

Держать их в отдельном модуле, а не в ``main.py``, нужно, чтобы роутеры могли
их импортировать, не импортируя приложение, — иначе получается цикл.
"""

from brokers.kafka import KafkaEventBroker
from services.event_service import EventService
from services.fallback_buffer import FallbackBuffer

broker: KafkaEventBroker | None = None
fallback_buffer: FallbackBuffer | None = None
event_service: EventService | None = None


def get_event_service() -> EventService:
    """Зависимость FastAPI: прикладной сервис событий."""
    if event_service is None:  # pragma: no cover — возможно только при ошибке сборки приложения
        raise RuntimeError('EventService is not initialized: lifespan did not run')
    return event_service


def get_broker() -> KafkaEventBroker | None:
    return broker


def get_fallback_buffer() -> FallbackBuffer | None:
    return fallback_buffer
