"""Необязательное обогащение позиций карточками из Movies API.

ПО УМОЛЧАНИЮ ВЫДАЧА ОТДАЁТ ГОЛЫЕ ИДЕНТИФИКАТОРЫ, и это требование F2.5, а не
экономия: каталог принадлежит Movies API, у него уже есть владелец, свой кэш и
свой индекс. Витрина, хранящая названия, — это второй каталог, который начнёт
расходиться с первым в тот день, когда фильм переименуют.

Тогда зачем этот модуль. Затем, что стык «блок рекомендаций → каталог» должен
быть проверяемым (E7), а не описанным словами: ``?enrich=true`` дорисовывает
названия по-настоящему, и функциональный тест это видит.

Почему он не на горячем пути. У Movies API нет ручки «карточки по списку
идентификаторов», поэтому N позиций — это N запросов. На плановых 100 RPS с
блоком из двадцати позиций это 2000 RPS в каталог ради данных, которые фронт и
так запрашивает сам, отрисовывая страницу. Отсюда потолок ``RECS_ENRICH_MAX``
и выключенность по умолчанию.

ЛЮБАЯ ОШИБКА ЗДЕСЬ — НЕ ОШИБКА ОТВЕТА. Каталог недоступен, ответил 404 на
удалённый фильм, не уложился в таймаут — позиция просто остаётся без названия,
и это ровно та деградация, ради которой поле ``title`` объявлено необязательным.
Уронить блок рекомендаций из-за недоступности украшения было бы обменом наоборот.
"""

import asyncio
import logging
import uuid

import httpx

from practix_core.context import REQUEST_ID_HEADER, get_request_id
from practix_recommendations_api.core.config import settings
from practix_recommendations_api.services import metrics

logger = logging.getLogger(__name__)


class CatalogClient:
    """Тонкий клиент Movies API. Живёт весь срок процесса — соединения переиспользуются."""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.RECS_MOVIES_API_URL.rstrip('/'),
            timeout=settings.RECS_MOVIES_API_TIMEOUT,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(self, film_ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[str | None, float | None]]:
        """Карточки по идентификаторам. Чего не удалось получить — того просто нет в ответе.

        Запросы идут параллельно и ограничены ``RECS_ENRICH_MAX``: последовательный
        цикл на двадцати позициях сложил бы двадцать таймаутов в один худший
        случай и пробил бы SLO по хвосту (p99 < 300 мс) даже при живом каталоге.
        """
        wanted = film_ids[: settings.RECS_ENRICH_MAX]
        if not wanted:
            return {}
        results = await asyncio.gather(*(self._fetch_one(film_id) for film_id in wanted))
        return {film_id: card for film_id, card in results if card is not None}

    async def _fetch_one(self, film_id: uuid.UUID) -> tuple[uuid.UUID, tuple[str | None, float | None] | None]:
        # X-Request-Id обязателен: RequestIdMiddleware в Movies API работает в
        # режиме reject_400 и без заголовка вернул бы 400 на каждый запрос.
        headers = {REQUEST_ID_HEADER: get_request_id() or str(uuid.uuid4())}
        try:
            response = await self._client.get(f'/api/v1/films/{film_id}', headers=headers)
        except httpx.HTTPError:
            metrics.enrich_failures.labels(reason='transport').inc()
            logger.warning('ENRICH FAILED: каталог недоступен, отдаём позицию без названия')
            return film_id, None

        if response.status_code != httpx.codes.OK:
            # 404 здесь штатен: витрина обучена на снимке каталога, а фильм с
            # тех пор могли снять с показа.
            metrics.enrich_failures.labels(reason=f'http_{response.status_code}').inc()
            return film_id, None

        try:
            payload = response.json()
        except ValueError:
            metrics.enrich_failures.labels(reason='decode').inc()
            return film_id, None

        return film_id, (payload.get('title'), payload.get('imdb_rating'))
