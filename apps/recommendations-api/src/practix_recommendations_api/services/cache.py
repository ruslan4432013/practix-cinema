"""Горячий слой витрины: версия батча входит в ключ (ADR-004).

Почему версия в ключе, а не инвалидация. Батч перекладывает витрину целиком.
Массовое удаление ключей означало бы, что между `DEL` и прогревом весь трафик
проваливается в PostgreSQL — то есть плановая операция сама себе создаёт пик,
причём ровно того размера, от которого кэш и защищает. С версией в ключе
переключение — это смена одного указателя, а старые ключи уходят по TTL, никого
не разбудив.

Обновление на месте отвергнуто отдельно: оно нарушило бы F0.2 — выдача увидела
бы половину нового батча поверх половины старого.

НИ ОДИН ОТКАЗ REDIS НЕ ПОДНИМАЕТСЯ ВЫШЕ. Горячий слой — ускорение, а не
источник истины: его падение обязано означать «медленнее», а не «ошибка». Вызов
получает ``None`` и идёт в PostgreSQL, а факт отказа виден в счётчике.

НО «МЕДЛЕННЕЕ» ТОЖЕ ИМЕЕТ ЦЕНУ, И ЕЁ НАДО ОГРАНИЧИТЬ. Погашенный контейнер
Redis не отказывает — он молчит, и клиент ждёт таймаут, умножая его на число
повторов подключения. На один ответ приходится два обращения к кэшу (указатель
и список), и вдвоём они съедали весь бюджет запроса раньше, чем очередь
доходила до PostgreSQL: выдача отдавала пустое популярное вместо честного
чтения витрины. То есть отказ ускорителя отменял работу источника истины.

Поэтому здесь предохранитель: первая же неудача гасит кэш на
``RECS_CACHE_FUSE_SECONDS``, и всё это время обращения к Redis не делаются
вовсе. Отказ стоит одного таймаута в несколько секунд, а не двух на каждый
запрос. Тот же приём и по той же причине применён к рассылке писем в
нотификациях (``channels/pacing.py``), где недоступный Redis иначе стоил бы
таймаута на письмо.
"""

import asyncio
import json
import logging
import time
import uuid

from redis.asyncio import Redis

from practix_recommendations_api.core.config import settings
from practix_recommendations_api.services import metrics

logger = logging.getLogger(__name__)

POINTER_KEY = 'recs:pointer'

Scored = list[tuple[uuid.UUID, float]]


def similar_key(version: int, film_id: uuid.UUID) -> str:
    return f'recs:v{version}:similar:{film_id}'


def personal_key(version: int, user_id: uuid.UUID) -> str:
    return f'recs:v{version}:personal:{user_id}'


def popular_key(version: int) -> str:
    return f'recs:v{version}:popular'


def encode(items: Scored) -> str:
    """Список пар в JSON. Пары, а не объекты: ключи полей заняли бы больше места."""
    return json.dumps([[str(film_id), score] for film_id, score in items])


def decode(raw: str) -> Scored | None:
    """Разбор значения из кэша. Битое значение — это промах, а не 500.

    В кэше может оказаться что угодно: чужой ключ, обрезанная запись, значение
    от прошлой несовместимой версии формата. Единственный правильный ответ на
    это — сходить в базу, а не уронить запрос.
    """
    try:
        payload = json.loads(raw)
        return [(uuid.UUID(str(film_id)), float(score)) for film_id, score in payload]
    except (ValueError, TypeError):
        logger.warning('CACHE DECODE FAILED: значение в кэше не разобрано, читаем витрину')
        return None


class ShelfCache:
    """Горячий слой. ``client is None`` — законное состояние: Redis не поднят."""

    def __init__(self, client: Redis | None) -> None:
        self._client = client
        # Номер версии кэшируется в памяти процесса на RECS_POINTER_TTL.
        # Без этого каждый запрос платил бы лишний round-trip к Redis только
        # ради числа, которое меняется раз в сутки.
        self._pointer: int | None = None
        self._pointer_read_at: float = 0.0
        # Предохранитель: до этого момента Redis не трогаем вовсе.
        self._fuse_until: float = 0.0

    @property
    def _blown(self) -> bool:
        return time.monotonic() < self._fuse_until

    def _blow_fuse(self) -> None:
        self._fuse_until = time.monotonic() + settings.RECS_CACHE_FUSE_SECONDS
        # Номер версии из памяти тоже сбрасываем: пока кэш погашен, версию
        # придётся читать из базы, и подсунуть сюда устаревшее число значило бы
        # отдавать строки версии, которой уже нет.
        self._pointer = None

    async def get_pointer(self) -> int | None:
        """Номер актуальной версии из Redis, с коротким кэшем в памяти процесса."""
        if self._pointer is not None and (time.monotonic() - self._pointer_read_at) < settings.RECS_POINTER_TTL:
            return self._pointer
        raw = await self._get(POINTER_KEY, operation='pointer')
        if raw is None:
            return None
        try:
            self._pointer = int(raw)
        except (TypeError, ValueError):
            return None
        self._pointer_read_at = time.monotonic()
        return self._pointer

    async def get_list(self, key: str, *, kind: str) -> Scored | None:
        raw = await self._get(key, operation='read')
        if raw is None:
            metrics.cache_misses.labels(kind=kind).inc()
            return None
        items = decode(raw)
        if items is None:
            metrics.cache_misses.labels(kind=kind).inc()
            return None
        metrics.cache_hits.labels(kind=kind).inc()
        return items

    async def put_list(self, key: str, items: Scored) -> None:
        """Заполнение по промаху. Пустой список тоже кэшируется — намеренно.

        Иначе фильм без соседей (а таких по SLO до 40% каталога) на каждом
        запросе ходил бы в PostgreSQL: промах кэша по отсутствующему ключу
        неотличим от промаха по непрогретому.
        """
        if self._client is None or self._blown:
            return
        try:
            async with asyncio.timeout(settings.RECS_REDIS_TIMEOUT):
                await self._client.set(key, encode(items), ex=settings.RECS_CACHE_TTL)
        except Exception:  # noqa: BLE001 — запись в кэш не имеет права ломать ответ
            metrics.cache_unavailable.labels(operation='write').inc()
            self._blow_fuse()

    async def _get(self, key: str, *, operation: str) -> str | None:
        if self._client is None or self._blown:
            metrics.cache_unavailable.labels(operation=operation).inc()
            return None
        try:
            # Собственный срок поверх таймаута клиента: redis-py умножает свой
            # на число повторов подключения, и на погашенном контейнере два
            # обращения к кэшу съедали весь бюджет запроса.
            async with asyncio.timeout(settings.RECS_REDIS_TIMEOUT):
                return await self._client.get(key)
        except Exception:  # noqa: BLE001 — отказ кэша означает «медленнее», а не «ошибка»
            metrics.cache_unavailable.labels(operation=operation).inc()
            self._blow_fuse()
            logger.warning(
                'CACHE UNAVAILABLE: горячий слой недоступен, читаем витрину из PostgreSQL; '
                'обращения к нему выключены на %s с',
                settings.RECS_CACHE_FUSE_SECONDS,
            )
            return None
