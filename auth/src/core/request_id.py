"""Middleware для обязательного заголовка ``X-Request-Id``.

Каждый входящий запрос обязан содержать ``X-Request-Id`` (его проставляет
Nginx). Запрос без него отклоняется с 400 — это гарантирует, что любой сбой
можно проаудировать по единому идентификатору. Идентификатор кладётся в
``ContextVar`` и в тег активного span'а, а также возвращается клиенту в
ответном заголовке.
"""

from contextvars import ContextVar

from opentelemetry import trace
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

REQUEST_ID_HEADER = 'X-Request-Id'

request_id_ctx: ContextVar[str | None] = ContextVar('request_id', default=None)


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get(REQUEST_ID_HEADER)
        if not request_id:
            # Отклоняем ДО какой-либо работы (жёсткая мера аудита, см. ТЗ).
            return JSONResponse(
                status_code=400,
                content={'detail': f'{REQUEST_ID_HEADER} header is required'},
            )
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
