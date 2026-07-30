"""Middleware сквозного идентификатора запроса ``X-Request-Id``.

ОТЛИЧИЕ ОТ СИБЛИНГОВ. В ``rest/`` и ``auth/`` отсутствие заголовка приводит к
400 — там все вызовы идут либо через Nginx, либо между сервисами, и жёсткое
требование оправдано. Здесь ручка публичная и вызывается из браузера, в том
числе через ``navigator.sendBeacon``, который **не умеет ставить произвольные
заголовки**. Требование заголовка означало бы, что beacon-события (а это
основной способ отправить «время на странице» при уходе с неё) никогда не
доедут. Поэтому идентификатор генерируется, если его нет.

Потери аудита не происходит: Nginx проставляет ``X-Request-Id`` для всего
внешнего трафика (см. nginx/nginx.conf) и перезаписывает присланное клиентом
значение, так что сгенерированный здесь UUID появляется только при прямом
обращении к сервису внутри сети.
"""

import uuid
from contextvars import ContextVar

from opentelemetry import trace
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

REQUEST_ID_HEADER = 'X-Request-Id'
# Ограничение длины: значение уходит в заголовок Kafka и в логи, поэтому
# присланная клиентом строка не должна быть неограниченной.
MAX_REQUEST_ID_LENGTH = 128

# Идентификатор текущего запроса. Читается логгером и продюсером Kafka.
request_id_ctx: ContextVar[str | None] = ContextVar('request_id', default=None)


def get_request_id() -> str | None:
    """Возвращает идентификатор текущего запроса (или None вне запроса)."""
    return request_id_ctx.get()


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Проставляет ``X-Request-Id`` в контекст, span и ответ."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get(REQUEST_ID_HEADER)
        if request_id:
            # Значение приходит извне — усекаем и вычищаем управляющие символы,
            # иначе им можно было бы «отравить» лог или заголовок Kafka.
            request_id = ''.join(ch for ch in request_id[:MAX_REQUEST_ID_LENGTH] if ch.isprintable())
        if not request_id:
            request_id = str(uuid.uuid4())

        span = trace.get_current_span()
        if span is not None and span.is_recording():
            span.set_attribute('http.request_id', request_id)

        token = request_id_ctx.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_ctx.reset(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
