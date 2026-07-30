"""Клиент Auth-сервиса для межсервисной проверки прав.

Ключевая ответственность модуля — изящная деградация: если Auth-сервис
недоступен (таймаут, сетевая ошибка, 5xx), клиент НИКОГДА не бросает
исключение наружу, а возвращает ``None``. Вызывающий код трактует ``None``
как «Auth недоступен» и опирается на данные из самого JWT-токена.

Дополнительно реализован примитивный circuit breaker поверх Redis: после
серии подряд неудачных обращений запросы к Auth-сервису временно
прекращаются, чтобы не добивать уже упавший сервис лавиной ретраев.
"""

import asyncio
import contextlib
import logging

import httpx
from redis.asyncio import Redis

from core.config import settings
from core.request_id import REQUEST_ID_HEADER, request_id_ctx

logger = logging.getLogger(__name__)

# Единый httpx-клиент на всё приложение (создаётся в lifespan, см. main.py).
client: httpx.AsyncClient | None = None

_CB_KEY = 'auth:cb:failures'
_CHECK_PERMISSIONS_PATH = '/api/v1/users/check-permissions'
_MAX_ATTEMPTS = 2
_RETRY_BASE_DELAY = 0.1


def get_auth_client() -> httpx.AsyncClient | None:
    """Возвращает разделяемый httpx-клиент (для внедрения зависимостей)."""
    return client


class AuthServiceClient:
    """Обёртка над httpx-клиентом с ретраями и circuit breaker'ом."""

    def __init__(self, http_client: httpx.AsyncClient | None, redis: Redis):
        self._client = http_client
        self._redis = redis

    async def _breaker_open(self) -> bool:
        """True, если circuit breaker разомкнут (Auth временно не опрашиваем)."""
        try:
            failures = await self._redis.get(_CB_KEY)
        except Exception:  # noqa: BLE001 — проблемы с Redis не должны ломать основной поток
            return False
        return failures is not None and int(failures) >= settings.auth_cb_failure_threshold

    async def _record_failure(self) -> None:
        try:
            pipe = self._redis.pipeline()
            pipe.incr(_CB_KEY)
            pipe.expire(_CB_KEY, settings.auth_cb_reset_seconds)
            await pipe.execute()
        except Exception:  # noqa: BLE001 — счётчик отказов не важнее самого запроса
            pass

    async def _record_success(self) -> None:
        with contextlib.suppress(Exception):
            await self._redis.delete(_CB_KEY)

    async def check_permissions(self, access_token: str, required_roles: list[str]) -> dict | None:
        """Проверяет права пользователя в Auth-сервисе.

        Возвращает тело ответа Auth-сервиса
        (``{allowed, user_id, roles, is_superuser}``) при HTTP 200,
        либо ``None``, если Auth-сервис недоступен / вернул 5xx / отключён
        флагом ``auth_check_enabled`` / разомкнут circuit breaker.
        """
        if not settings.auth_check_enabled or self._client is None:
            return None

        if await self._breaker_open():
            logger.warning('Circuit breaker открыт — пропускаем запрос к Auth-сервису')
            return None

        payload = {'access_token': access_token, 'required_roles': required_roles}
        url = settings.auth_api_url.rstrip('/') + _CHECK_PERMISSIONS_PATH

        # Auth-сервис требует X-Request-Id, поэтому прокидываем идентификатор
        # текущего запроса на межсервисный вызов (traceparent OTel добавит сам).
        rid = request_id_ctx.get()
        headers = {REQUEST_ID_HEADER: rid} if rid else {}

        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = await self._client.post(
                    url, json=payload, headers=headers, timeout=settings.auth_request_timeout
                )
            except httpx.HTTPError as exc:
                logger.warning('Auth-сервис недоступен (%s), попытка %d', exc, attempt + 1)
            else:
                if response.status_code >= 500:
                    logger.warning('Auth-сервис вернул %d, попытка %d', response.status_code, attempt + 1)
                elif response.status_code == 200:
                    await self._record_success()
                    return response.json()
                else:
                    # 4xx — валидный ответ Auth-сервиса (не сбой инфраструктуры),
                    # ретраи не нужны, деградация тоже.
                    await self._record_success()
                    return response.json() if response.content else None

            # Неудача: наивная задержка перед следующей попыткой.
            if attempt + 1 < _MAX_ATTEMPTS:
                await asyncio.sleep(_RETRY_BASE_DELAY * (attempt + 1))

        await self._record_failure()
        return None
