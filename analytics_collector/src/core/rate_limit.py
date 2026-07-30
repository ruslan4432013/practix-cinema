"""Ограничение частоты запросов (rate limiting).

Алгоритм — **скользящее окно с весами** на Redis. Раньше здесь было
фиксированное окно (как в Auth): счётчик сбрасывался в ноль на границе окна, и
клиент, попавший в стык, отправлял до двух лимитов подряд — сначала весь остаток
одного окна, затем сразу весь следующий. Для ingest-ручки это вдвое больший
всплеск, чем разрешено, ровно в момент, когда защита и нужна.

Скользящее окно считается по двум соседним корзинам: полный счётчик текущей
плюс доля предыдущей, пропорциональная тому, какая её часть ещё попадает в окно.
Это классическая аппроксимация: она стоит те же две операции Redis, что и
фиксированное окно (полноценный sliding log потребовал бы хранить отметку
времени каждого запроса), но убирает всплеск на границе.

ЧЕТЫРЕ ОТЛИЧИЯ ОТ ВЕРСИИ В AUTH:

1. **Стоимость запроса переменная.** Батч из 50 событий нагружает Kafka так же,
   как 50 одиночных запросов, поэтому и «стоить» он должен 50. Иначе лимит
   тривиально обходится упаковкой событий в пачки. Базовую единицу списывает
   middleware, добавочный вес — сама батч-ручка (``consume``).
2. **Лимит не только по IP.** Один IP слаб против ботнета и, наоборот, бьёт по
   легитимным пользователям за общим NAT. Поэтому ручки дополнительно
   лимитируют по ``anonymous_id`` и ``session_id`` — см. ``api/v1/events.py``.
3. **Списание нескольких идентичностей — одно обращение к Redis**
   (``consume_many``). На ingest-ручке это горячий путь: раньше на каждое
   событие приходилось до трёх ПОСЛЕДОВАТЕЛЬНЫХ round-trip (IP в middleware,
   затем ``anonymous_id``, затем ``session_id``), и их задержки складывались.
   Теперь все ключи одного запроса едут в Redis одним pipeline, то есть за один
   round-trip независимо от их числа.
4. **Fail-open при недоступном Redis.** В Auth отказ Redis логично трактовать
   строго, здесь — нет: аналитика не должна ломать сайт, поэтому недоступность
   Redis приводит к пропуску запроса, а не к отказу.
"""

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass

from redis.asyncio import Redis
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from core.config import settings
from core.privacy import get_client_ip

logger = logging.getLogger(__name__)

# Служебные пути не лимитируются: healthcheck'и Docker/Kubernetes и скрейп
# Prometheus идут с высокой частотой и не являются пользовательским трафиком.
EXEMPT_PATH_PREFIXES = ('/health', '/metrics', '/api/analytics/openapi')

# Число команд Redis на одно списание: INCRBY + EXPIRE + GET. Используется для
# разбора плоского ответа pipeline.
_COMMANDS_PER_CHARGE = 3


@dataclass(frozen=True, slots=True)
class Charge:
    """Одно списание: с кого, сколько и по какому порогу."""

    identity: str
    cost: int = 1
    # None означает общий порог UGC_RATE_LIMIT_TIMES.
    limit: int | None = None
    # False — «списать, но не отклонять». Так ведёт себя добавочный вес пачки:
    # событие уже принято, отказывать в нём задним числом бессмысленно, а вот
    # исчерпанный лимит обязан сказаться на СЛЕДУЮЩЕМ запросе клиента.
    enforce: bool = True


class RateLimiter:
    """Счётчик запросов в скользящем окне, общий для всех инстансов."""

    def __init__(self, redis: Redis):
        self._redis = redis

    async def consume(self, identity: str, cost: int = 1, limit: int | None = None) -> tuple[bool, int]:
        """Списывает ``cost`` единиц лимита у ``identity``.

        ``limit`` позволяет ручке задать свой порог (у идентификатора браузера
        он ниже, чем у IP). Возвращает ``(allowed, retry_after_seconds)``.
        При недоступном Redis запрос разрешается — см. docstring модуля.
        """
        return await self.consume_many([Charge(identity=identity, cost=cost, limit=limit)])

    async def consume_many(self, charges: Sequence[Charge]) -> tuple[bool, int]:
        """Списывает несколько идентичностей за ОДИН round-trip к Redis.

        Возвращает ``(allowed, retry_after_seconds)`` по всем списаниям сразу:
        запрос отклоняется, если порог превысила хотя бы одна идентичность с
        ``enforce=True``.

        Списание применяется ко всем ключам независимо от результата — иначе
        клиент, упёршийся в один из лимитов, «бесплатно» тратил бы остальные.
        """
        if not settings.UGC_RATE_LIMIT_ENABLED or not charges:
            return True, 0

        window = settings.UGC_RATE_LIMIT_SECONDS
        now = time.time()
        bucket = int(now) // window
        # Какая доля текущей корзины уже прошла: 0.0 в её начале, ~1.0 в конце.
        elapsed_ratio = (now - bucket * window) / window

        try:
            pipe = self._redis.pipeline()
            for charge in charges:
                current_key = f'ugc:ratelimit:{charge.identity}:{bucket}'
                pipe.incrby(current_key, charge.cost)
                # TTL ставится только при создании ключа: иначе каждый запрос
                # продлевал бы окно, и при непрерывном потоке счётчик никогда бы
                # не истёк. Живём два окна — предыдущая корзина нужна для веса.
                pipe.expire(current_key, window * 2, nx=True)
                pipe.get(f'ugc:ratelimit:{charge.identity}:{bucket - 1}')
            result = await pipe.execute()
        except Exception as exc:  # noqa: BLE001 — Redis не должен ломать приём событий
            logger.warning('Rate limit check skipped, Redis unavailable: %s', exc)
            return True, 0

        # Ждать до конца текущей корзины: к этому моменту вклад предыдущей
        # обнулится, а сама она станет предыдущей с убывающим весом.
        retry_after = max(1, int(window * (1.0 - elapsed_ratio)) + 1)
        allowed = True
        for index, charge in enumerate(charges):
            offset = index * _COMMANDS_PER_CHARGE
            current = int(result[offset])
            previous = int(result[offset + 2] or 0)
            # Вес предыдущей корзины убывает по мере продвижения по текущей.
            weighted = current + previous * (1.0 - elapsed_ratio)
            threshold = settings.UGC_RATE_LIMIT_TIMES if charge.limit is None else charge.limit
            if weighted > threshold and charge.enforce:
                allowed = False

        return (True, 0) if allowed else (False, retry_after)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Списывает одну единицу лимита за каждый входящий запрос."""

    def __init__(self, app, redis_provider):
        """``redis_provider`` — вызываемое, возвращающее клиент Redis.

        Клиент создаётся в ``lifespan`` уже после конструирования middleware,
        поэтому берём его лениво, а не сохраняем ссылку на этапе сборки app.
        """
        super().__init__(app)
        self._redis_provider = redis_provider

    async def dispatch(self, request: Request, call_next):
        if not settings.UGC_RATE_LIMIT_ENABLED:
            return await call_next(request)
        if request.url.path.startswith(EXEMPT_PATH_PREFIXES):
            return await call_next(request)

        redis = self._redis_provider()
        if redis is None:
            return await call_next(request)

        ip = get_client_ip(
            {name.lower(): value for name, value in request.headers.items()},
            request.client.host if request.client else None,
        )
        allowed, retry_after = await RateLimiter(redis).consume(ip or 'unknown')
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={'detail': 'Too Many Requests'},
                headers={'Retry-After': str(retry_after)},
            )
        return await call_next(request)
