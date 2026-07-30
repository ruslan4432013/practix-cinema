"""Middleware для обязательного заголовка ``X-Request-Id``.

Запрос без ``X-Request-Id`` (его проставляет Nginx) отклоняется с 400.
Идентификатор проставляется тегом ``http.request_id`` на активный span,
созданный ``DjangoInstrumentor``, и возвращается клиенту в ответном заголовке.
"""

from django.http import JsonResponse
from opentelemetry import trace

# WSGI нормализует заголовок X-Request-Id в ключ HTTP_X_REQUEST_ID.
REQUEST_ID_META_KEY = 'HTTP_X_REQUEST_ID'
REQUEST_ID_HEADER = 'X-Request-Id'


class RequestIdMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = request.META.get(REQUEST_ID_META_KEY)
        if not request_id:
            return JsonResponse({'detail': f'{REQUEST_ID_HEADER} header is required'}, status=400)
        span = trace.get_current_span()
        if span is not None and span.is_recording():
            span.set_attribute('http.request_id', request_id)
        response = self.get_response(request)
        response[REQUEST_ID_HEADER] = request_id
        return response
