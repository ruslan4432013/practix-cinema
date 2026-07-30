"""Контекст текущего запроса — без внешних зависимостей.

Модуль отделён от ``practix_core.request_id`` СПЕЦИАЛЬНО. Middleware требует
``starlette`` и ``opentelemetry-api``, а ``practix_core.logging`` нужен только сам
``ContextVar``. Если бы они лежали вместе, логгер фонового ETL (у которого нет ни
HTTP-слоя, ни starlette в образе) тянул бы starlette транзитивно — и падал бы на
``ImportError`` при первой же настройке логирования.

Здесь не должно появиться ни одного импорта, кроме stdlib.
"""

from contextvars import ContextVar

REQUEST_ID_HEADER = 'X-Request-Id'

# Идентификатор текущего запроса. Читают логгер, продюсер Kafka и исходящие
# межсервисные клиенты. ContextVar один на процесс и живёт здесь, чтобы у
# сервисов не оказалось двух несвязанных контекстов.
request_id_ctx: ContextVar[str | None] = ContextVar('request_id', default=None)


def get_request_id() -> str | None:
    """Возвращает идентификатор текущего запроса (или None вне запроса)."""
    return request_id_ctx.get()
