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

from practix_auth.core.config import settings
from practix_auth.db.redis import get_redis
from practix_core.net import is_trusted_proxy as _is_trusted_proxy
from practix_core.net import resolve_client_ip

# Пути, которые не участвуют в rate limit (документация / OpenAPI-схема).
_EXEMPT_PATH_PREFIX = '/api/openapi'

# Ключ, под которым считаются запросы, если адрес определить не удалось. Раньше
# такие запросы получали строку 'unknown' — оставляем то же поведение, чтобы они
# делили общий счётчик, а не проходили лимит вовсе.
_UNKNOWN_CLIENT = 'unknown'


def is_trusted_proxy(peer: str | None) -> bool:
    """Пришло ли соединение от известного прокси (сети — из настроек Auth)."""
    return _is_trusted_proxy(peer, settings.trusted_proxy_networks)


def get_client_ip(request: Request) -> str:
    """Определяет IP клиента с учётом того, что сервис стоит за Nginx.

    Алгоритм — в ``practix_core.net``: там же объяснено, почему берётся ПОСЛЕДНИЙ
    хоп ``X-Forwarded-For``, а не первый (первый подставляет сам клиент, и на нём
    защита от брутфорса обходилась одним заголовком). Тот же код обслуживает
    коллектор, где он дополнительно защищает ``ip_hash`` в аналитике.
    """
    peer = request.client.host if request.client else None
    return resolve_client_ip(request.headers, peer, settings.trusted_proxy_networks) or _UNKNOWN_CLIENT


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
