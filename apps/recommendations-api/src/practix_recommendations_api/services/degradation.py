"""Лестница деградации — единственное место, где решается, что отдать.

Раздел 5.2 ТЗ формулирует инвариант так: ни один отказ не имеет права
превратиться в 5xx на странице фильма. Здесь он и живёт целиком, а не
размазан по роутерам:

1. горячий слой Redis жив — отдаём из него;
2. Redis лёг — читаем витрину из PostgreSQL, это медленнее, но это ответ;
3. PostgreSQL лёг — отдаём последнее известное популярное из памяти процесса;
4. витрины нет / соседей нет / у пользователя нет истории — отдаём популярное,
   **200, а не 404 и не 5xx** (F1.3);
5. и популярного ещё нет (первый запуск, обучение не проходило) — 200 с пустым
   списком и источником ``popular``.

ОБЩИЙ БЮДЖЕТ ВРЕМЕНИ — ПЯТАЯ, НЕЯВНАЯ СТУПЕНЬ, и без неё все четыре
предыдущие не работают в самом важном сценарии. Отказ источника бывает двух
видов: он отвечает ошибкой (тогда срабатывает ``except`` ниже) и он НЕ
отвечает вовсе. Второй случай — остановленный контейнер, разрыв сети,
переполненный пул — не даёт исключения, он даёт ожидание, и лестница просто не
доходит до своей следующей ступени. Хуже того, ожидания складываются: клиент
Redis умножает свой таймаут на число повторов, к нему прибавляется таймаут
базы, и суммарно запрос перекрывает ``proxy_read_timeout`` nginx. Пользователь
получает 504 вместо 200 с популярным — то есть ровно то, чего раздел 5.2
запрещает. Поймано на стенде: погашенные recs-db и redis-recs давали 404 от
nginx при формально исправном коде.

Поэтому у обработчика есть ЖЁСТКИЙ срок, заведомо меньший таймаута nginx, и
его истечение — это не ошибка, а та же деградация: отдаём последнее известное
популярное.

Единственный 404 в сервисе — неизвестный ``film_id`` (F2.4), и он возможен
только когда витрина ЖИВА и точно знает состав каталога. Если базы нет,
доказать отсутствие фильма нечем, и «не знаю» отдаётся как популярное, а не
как 404: выдумывать отсутствие по недоступности источника — худший вид лжи в
API.

ПОЧЕМУ ПРОВЕРКА КАТАЛОГА ИДЁТ ПОСЛЕ ЧТЕНИЯ СПИСКА, А НЕ ДО. Она стоит поход в
PostgreSQL, а на счастливом пути (соседи нашлись в Redis) её ответ не нужен:
если у фильма есть соседи, он заведомо существует. Проверять заранее значило бы
платить за 404 на каждом успешном запросе.
"""

import asyncio
import logging
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass

from practix_recommendations_api.core.config import settings
from practix_recommendations_api.models.schemas import RecommendationSource
from practix_recommendations_api.services import metrics
from practix_recommendations_api.services.cache import Scored, ShelfCache, personal_key, popular_key, similar_key
from practix_recommendations_api.services.shelf import EMPTY_SHELF, ShelfReader, ShelfState

logger = logging.getLogger(__name__)


class FilmNotFound(Exception):
    """Фильма нет в каталоге витрины — единственный повод ответить 404."""


@dataclass(frozen=True, slots=True)
class Recommendations:
    source: RecommendationSource
    version: int | None
    items: Scored


class LastKnownPopular:
    """Последнее успешно прочитанное популярное, в памяти процесса.

    Это ступень 3 лестницы: PostgreSQL недоступен, Redis тоже, а блок на
    странице фильма всё равно должен отрисоваться. Список короткий (десятки
    позиций), живёт до перезапуска воркера и намеренно не имеет TTL: протухшее
    популярное лучше пустого экрана, а «протухшее» здесь означает «вчерашнее».

    Своё на каждый воркер uvicorn, и это осознанно: общий кэш означал бы ещё
    один сетевой поход ровно в тот момент, когда сеть уже подвела.
    """

    def __init__(self) -> None:
        self._items: Scored = []
        self._version: int | None = None

    def remember(self, version: int | None, items: Scored) -> None:
        if items:
            self._items = items
            self._version = version

    def recall(self) -> tuple[int | None, Scored]:
        return self._version, list(self._items)


class RecommendationService:
    """Собирает ответ, спускаясь по лестнице до первого источника, который жив."""

    def __init__(self, reader: ShelfReader, cache: ShelfCache, last_known: LastKnownPopular) -> None:
        self._reader = reader
        self._cache = cache
        self._last_known = last_known

    # --- Публичные сценарии -------------------------------------------------

    async def similar(self, film_id: uuid.UUID, limit: int) -> Recommendations:
        return await self._within_budget(self._similar(film_id, limit), limit=limit, requested='similar')

    async def personal(self, user_id: uuid.UUID, limit: int) -> Recommendations:
        return await self._within_budget(self._personal(user_id, limit), limit=limit, requested='personal')

    async def popular(self, limit: int) -> Recommendations:
        return await self._within_budget(self._popular_only(limit), limit=limit, requested='popular')

    async def _within_budget(
        self,
        work: Coroutine[None, None, Recommendations],
        *,
        limit: int,
        requested: str,
    ) -> Recommendations:
        """Срок на весь ответ. Истёк — отдаём последнее известное популярное.

        ``FilmNotFound`` проходит насквозь: 404 — это ответ по существу, а не
        отказ источника, и подменять его популярным значило бы врать про состав
        каталога.
        """
        try:
            async with asyncio.timeout(settings.RECS_REQUEST_BUDGET):
                return await work
        except FilmNotFound:
            raise
        except TimeoutError:
            metrics.shelf_unavailable.inc()
            metrics.requests_total.labels(kind=requested, source='popular').inc()
            logger.warning('BUDGET EXCEEDED: источники не ответили за отведённое время, отдаём известное популярное')
            version, items = self._last_known.recall()
            return Recommendations(RecommendationSource.POPULAR, version, items[:limit])

    # --- Реализация сценариев ----------------------------------------------

    async def _similar(self, film_id: uuid.UUID, limit: int) -> Recommendations:
        state = await self._state()
        version = state.version
        if version is None:
            return await self._popular(state, limit, requested='similar')

        items = await self._read(
            key=similar_key(version, film_id),
            kind='similar',
            fetch=lambda: self._reader.similar(version, film_id, limit),
        )
        if items:
            metrics.requests_total.labels(kind='similar', source='similar').inc()
            return Recommendations(RecommendationSource.SIMILAR, version, items[:limit])

        # Соседей нет. Теперь — и только теперь — важно, существует ли фильм.
        await self._ensure_film_exists(version, film_id)
        return await self._popular(state, limit, requested='similar')

    async def _personal(self, user_id: uuid.UUID, limit: int) -> Recommendations:
        state = await self._state()
        version = state.version
        if version is None:
            return await self._popular(state, limit, requested='personal')

        items = await self._read(
            key=personal_key(version, user_id),
            kind='personal',
            fetch=lambda: self._reader.personal(version, user_id, limit),
        )
        if items:
            metrics.requests_total.labels(kind='personal', source='personal').inc()
            return Recommendations(RecommendationSource.PERSONAL, version, items[:limit])

        # Пользователь без истории — штатный случай, а не ошибка (F4.4).
        return await self._popular(state, limit, requested='personal')

    async def _popular_only(self, limit: int) -> Recommendations:
        return await self._popular(await self._state(), limit, requested='popular')

    # --- Внутренности лестницы ---------------------------------------------

    async def _state(self) -> ShelfState:
        """Номер версии: сначала указатель в Redis, потом указатель в базе.

        Указатель в Redis отвечает только на вопрос «какая версия», но не
        сообщает, когда она разложена, — а возраст витрины нужен метрике
        свежести (F0.4). Ходить за меткой времени в базу на каждом запросе
        значило бы отменить весь смысл кэша, поэтому возраст обновляет проба
        готовности: она и так читает состояние витрины раз в десять секунд, а
        суточному циклу обучения такой частоты хватает с запасом.
        """
        cached_version = await self._cache.get_pointer()
        if cached_version is not None:
            metrics.shelf_version.set(cached_version)
            return ShelfState(version=cached_version, finished_at=None, models='')

        try:
            state = await self._reader.get_state()
        except Exception:  # noqa: BLE001 — витрина недоступна: это ступень 3, а не 500
            metrics.shelf_unavailable.inc()
            logger.warning('SHELF UNAVAILABLE: витрина недоступна, отдаём последнее известное популярное')
            return EMPTY_SHELF

        if state.version is not None:
            metrics.shelf_version.set(state.version)
        age = state.age_seconds
        if age is not None:
            metrics.shelf_age.set(age)
        return state

    async def _read(self, *, key: str, kind: str, fetch) -> Scored:
        """Горячий слой, затем витрина. Отказ витрины — пустой список, не исключение."""
        cached = await self._cache.get_list(key, kind=kind)
        if cached is not None:
            return cached
        try:
            items = await fetch()
        except Exception:  # noqa: BLE001 — ступень 3 лестницы, см. докстринг модуля
            metrics.shelf_unavailable.inc()
            logger.warning('SHELF UNAVAILABLE: чтение витрины не удалось для %s', kind)
            return []
        await self._cache.put_list(key, items)
        return items

    async def _popular(self, state: ShelfState, limit: int, *, requested: str) -> Recommendations:
        version = state.version
        if version is not None:
            items = await self._read(
                key=popular_key(version),
                kind='popular',
                fetch=lambda: self._reader.popular(version, limit),
            )
            if items:
                self._last_known.remember(version, items)
                metrics.requests_total.labels(kind=requested, source='popular').inc()
                return Recommendations(RecommendationSource.POPULAR, version, items[:limit])

        # Ступень 3 и ступень 5 сходятся здесь: либо база недоступна, либо
        # обучение ещё не проходило. В обоих случаях ответ — 200.
        version, items = self._last_known.recall()
        metrics.requests_total.labels(kind=requested, source='popular').inc()
        return Recommendations(RecommendationSource.POPULAR, version, items[:limit])

    async def _ensure_film_exists(self, version: int, film_id: uuid.UUID) -> None:
        """404 только тогда, когда отсутствие фильма ДОКАЗАНО.

        Доказательством считается непустой снимок каталога этой версии, в
        котором фильма нет. Всё остальное — недоступная база, версия без
        снимка, устаревший указатель в кэше — это «не знаю», и отвечать на «не
        знаю» четырёхсоткой значит врать про состав каталога. Причём врать
        масштабно: в несуществующей версии нет НИ ОДНОГО фильма, так что один
        устаревший указатель превратил бы в 404 весь каталог сразу.
        """
        try:
            found, catalog_loaded = await self._reader.catalog_lookup(version, film_id)
        except Exception:  # noqa: BLE001 — доказать отсутствие нечем, значит 404 не имеем права
            metrics.shelf_unavailable.inc()
            return
        if catalog_loaded and not found:
            raise FilmNotFound
