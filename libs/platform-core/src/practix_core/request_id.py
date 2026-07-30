"""Middleware сквозного идентификатора запроса ``X-Request-Id``.

До вынесения модуль существовал в трёх копиях. ``rest/`` и ``auth/`` различались
ТОЛЬКО комментариями — ни одной отличающейся строки логики. Коллектор отличался
по существу, и это отличие здесь выражено параметрами, а не отдельным файлом:

* ``on_missing='reject_400'`` (по умолчанию) — поведение ``rest``/``auth``: все
  вызовы идут либо через Nginx, либо между сервисами, поэтому отсутствие
  заголовка — повод отказать до какой-либо работы и гарантировать аудит.
* ``on_missing='generate'`` — поведение коллектора: его ручка публичная и
  вызывается из браузера, в том числе через ``navigator.sendBeacon``, который
  **не умеет ставить произвольные заголовки**. Требование заголовка означало бы,
  что beacon-события (основной способ отправить «время на странице» при уходе с
  неё) никогда не доедут.
* ``sanitize=True`` — усечение и вычистка управляющих символов. Нужно там, где
  значение может прийти напрямую от клиента и уехать в лог или в заголовок
  Kafka; при ``reject_400`` за Nginx значение всегда своё.

Потери аудита при ``generate`` нет: Nginx проставляет ``X-Request-Id`` для всего
внешнего трафика (см. infra nginx.conf) и перезаписывает присланное клиентом
значение, так что сгенерированный UUID появляется только при прямом обращении к
сервису внутри сети.

ПОРЯДОК ПОДКЛЮЧЕНИЯ ВАЖЕН. ``app.add_middleware(RequestIdMiddleware)`` должен
идти ДО инструментации OpenTelemetry: та регистрируется внешним слоем, и
серверный span обязан существовать к моменту, когда мы ставим на него тег.
Переставь их местами — тег молча перестанет появляться, и ошибки не будет.
"""

import uuid
from typing import Literal

from opentelemetry import trace
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# ContextVar живёт в practix_core.context — модуле без внешних зависимостей,
# чтобы practix_core.logging мог его читать, не втягивая starlette в образ ETL.
from practix_core.context import REQUEST_ID_HEADER as REQUEST_ID_HEADER
from practix_core.context import get_request_id as get_request_id
from practix_core.context import request_id_ctx as request_id_ctx

# Значение уходит в заголовок Kafka и в логи, поэтому присланная клиентом строка
# не должна быть неограниченной.
MAX_REQUEST_ID_LENGTH = 128

OnMissing = Literal['reject_400', 'generate']


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Проставляет ``X-Request-Id`` в контекст, span и ответ."""

    def __init__(
        self,
        app,
        *,
        on_missing: OnMissing = 'reject_400',
        sanitize: bool = False,
        header: str = REQUEST_ID_HEADER,
        max_length: int = MAX_REQUEST_ID_LENGTH,
    ):
        super().__init__(app)
        self.on_missing = on_missing
        self.sanitize = sanitize
        self.header = header
        self.max_length = max_length

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get(self.header)

        if request_id and self.sanitize:
            request_id = ''.join(ch for ch in request_id[: self.max_length] if ch.isprintable())

        if not request_id:
            if self.on_missing == 'reject_400':
                # Отклоняем ДО какой-либо работы (жёсткая мера аудита).
                return JSONResponse(
                    status_code=400,
                    content={'detail': f'{self.header} header is required'},
                )
            request_id = str(uuid.uuid4())

        # Помечаем активный серверный span (он существует, т.к. middleware
        # OpenTelemetry — внешний по отношению к нашему).
        span = trace.get_current_span()
        if span is not None and span.is_recording():
            span.set_attribute('http.request_id', request_id)

        token = request_id_ctx.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_ctx.reset(token)
        response.headers[self.header] = request_id
        return response
