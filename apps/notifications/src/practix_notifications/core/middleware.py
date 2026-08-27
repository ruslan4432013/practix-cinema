"""Сквозной ``X-Request-Id`` для Django-панели.

Логика та же, что у ``practix_core.request_id.RequestIdMiddleware``, но взять её
как есть нельзя: та построена на ``starlette.middleware.base.BaseHTTPMiddleware``
и асинхронна, а здесь WSGI и синхронный стек. Общее — ``ContextVar`` и имя
заголовка — импортируется из ``practix_core.context``, поэтому расходиться могут
только два вызова, а не определение контекста.

РЕЖИМ «СГЕНЕРИРОВАТЬ, ЕСЛИ НЕТ», а не «отказать 400». Это отличие от
``apps/django-admin/example/middleware.py`` и оно вынужденное: та панель стоит за
Nginx, который проставляет заголовок всему внешнему трафику, а эта живёт в
compose-профиле и публикуется прямым хост-портом. Браузер ``X-Request-Id`` не
шлёт — с ``reject_400`` админка отвечала бы 400 на каждой странице, включая форму
логина.
"""

import uuid

from opentelemetry import trace

from practix_core.context import REQUEST_ID_HEADER, request_id_ctx

MAX_REQUEST_ID_LENGTH = 128


class RequestIdMiddleware:
    """Кладёт идентификатор запроса в ContextVar, span и заголовок ответа."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = request.headers.get(REQUEST_ID_HEADER) or ''
        # Значение приходит снаружи и уезжает в лог — усечь и вычистить
        # управляющие символы обязательно, иначе строка лога перестанет быть одной
        # строкой, а именно на это опирается разбор в Logstash.
        request_id = ''.join(ch for ch in request_id[:MAX_REQUEST_ID_LENGTH] if ch.isprintable())
        if not request_id:
            request_id = str(uuid.uuid4())

        span = trace.get_current_span()
        if span is not None and span.is_recording():
            span.set_attribute('http.request_id', request_id)

        token = request_id_ctx.set(request_id)
        try:
            response = self.get_response(request)
        finally:
            request_id_ctx.reset(token)
        response[REQUEST_ID_HEADER] = request_id
        return response
