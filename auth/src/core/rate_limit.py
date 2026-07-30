"""Middleware для ограничения количества запросов (rate limiting).

Реализует простой счётчик на Redis по алгоритму фиксированного окна
(``INCR`` + ``EXPIRE``): для каждого клиентского IP считается число запросов
в текущем временном окне. Как только лимит превышен, запрос отклоняется с
``429 Too Many Requests``. Состояние хранится в общем Redis, поэтому лимит
работает одинаково на всех инстансах сервиса.
"""

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from core.config import settings
from db.redis import get_redis

# Пути, которые не участвуют в rate limit (документация / OpenAPI-схема).
_EXEMPT_PATH_PREFIX = '/api/openapi'


def get_client_ip(request: Request) -> str:
    """Определяет реальный IP клиента.

    За Nginx ``request.client.host`` — это адрес прокси, поэтому берём первый
    хоп из ``X-Forwarded-For``, затем ``X-Real-IP`` (их проставляет Nginx),
    и лишь в крайнем случае — адрес соединения.
    """
    forwarded_for = request.headers.get('x-forwarded-for')
    if forwarded_for:
        return forwarded_for.split(',')[0].strip()
    real_ip = request.headers.get('x-real-ip')
    if real_ip:
        return real_ip
    return request.client.host if request.client else 'unknown'


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Ограничивает число запросов на клиентский IP в фиксированном окне."""

    async def dispatch(self, request: Request, call_next):
        if not settings.RATE_LIMIT_ENABLED:
            return await call_next(request)

        if request.url.path.startswith(_EXEMPT_PATH_PREFIX):
            return await call_next(request)

        window = settings.RATE_LIMIT_SECONDS
        bucket = int(time.time()) // window
        ip = get_client_ip(request)
        key = f'ratelimit:{ip}:{bucket}'

        redis = await get_redis()
        pipe = redis.pipeline()
        pipe.incr(key)
        # TTL выставляем только при создании ключа (nx=True): иначе каждый
        # запрос продлевал бы жизнь ключа до полного окна, и при непрерывном
        # спаме счётчик никогда бы не истёк.
        pipe.expire(key, window, nx=True)
        result = await pipe.execute()
        request_number = result[0]

        if request_number > settings.RATE_LIMIT_TIMES:
            return JSONResponse(
                status_code=429,
                content={'detail': 'Too Many Requests'},
                headers={'Retry-After': str(window)},
            )

        return await call_next(request)
