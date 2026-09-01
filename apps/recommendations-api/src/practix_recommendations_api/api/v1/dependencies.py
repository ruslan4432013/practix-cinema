"""Зависимости FastAPI: личность пользователя и сервисный слой.

``user_id`` берётся ИСКЛЮЧИТЕЛЬНО из подписи токена (F4.2). Параметром его
передать нельзя, и это не формальность: персональная выдача, принимающая чужой
идентификатор, — готовый оракул, по которому восстанавливается история
просмотров любого человека.

Проверка подписи локальная, общим секретом. Ходить в Auth за каждым запросом на
плановых 100 RPS мы не будем, но отозванный токен обязан перестать работать
немедленно — поэтому денилист читается из Redis Auth-сервиса, а политика при
его недоступности — ``deny`` (раздел 5.5 ТЗ). Это осознанно строже коллектора
(``allow``): там потерянное событие дешевле отказа, здесь на кону чужая
история просмотров.

Анонимные ручки (похожие, популярное) зависимостей аутентификации не имеют
вовсе — F1.2 требует отдавать популярное без токена.
"""

import uuid

from async_fastapi_jwt_auth import AuthJWT
from fastapi import Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from practix_recommendations_api.core.config import settings
from practix_recommendations_api.db.postgres import get_session
from practix_recommendations_api.db.redis import get_recs_redis
from practix_recommendations_api.services.cache import ShelfCache
from practix_recommendations_api.services.catalog_client import CatalogClient
from practix_recommendations_api.services.degradation import LastKnownPopular, RecommendationService
from practix_recommendations_api.services.shelf import ShelfReader

# Живут весь срок процесса, заполняются в lifespan. Пересоздавать их на каждый
# запрос значило бы выбрасывать ровно то, ради чего они существуют: у клиента
# каталога — пул соединений, у последнего известного популярного — накопленное
# состояние, у горячего слоя — запомненный номер версии витрины (иначе
# RECS_POINTER_TTL не экономил бы ничего: свежий объект каждый раз ходил бы за
# указателем в Redis заново).
catalog_client: CatalogClient | None = None
shelf_cache: ShelfCache | None = None
last_known_popular = LastKnownPopular()

AUTH_RESPONSES: dict[int | str, dict] = {
    status.HTTP_401_UNAUTHORIZED: {'description': 'Токен отсутствует, недействителен или отозван'},
}


def limit_param(
    limit: int | None = Query(
        default=None,
        ge=1,
        description='Сколько позиций вернуть. По умолчанию RECS_DEFAULT_LIMIT, потолок — RECS_MAX_LIMIT.',
    ),
) -> int:
    """Размер блока, обрезанный потолком.

    Потолок обрезает, а не отвергает 422: вёрстка блока — дело фронта, и
    завышенный ``limit`` не повод отказать в рекомендациях. Верхняя граница при
    этом обязана быть, иначе один запрос вытянет всю персональную выдачу.
    """
    return settings.clamp_limit(limit)


async def get_current_user_id(authorize: AuthJWT = Depends()) -> uuid.UUID:
    """Субъект из подписанного токена. Единственный источник ``user_id``."""
    await authorize.jwt_required()
    raw_jwt = await authorize.get_raw_jwt() or {}
    try:
        return uuid.UUID(str(raw_jwt.get('sub')))
    except (ValueError, TypeError) as exc:
        # sub есть, но это не UUID — токен выпущен не нашим Auth-сервисом.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Некорректный субъект токена') from exc


async def get_recommendation_service(db: AsyncSession = Depends(get_session)) -> RecommendationService:
    # Сессия базы — на запрос, кэш и память о популярном — на процесс.
    cache = shelf_cache if shelf_cache is not None else ShelfCache(await get_recs_redis())
    return RecommendationService(ShelfReader(db), cache, last_known_popular)


async def get_catalog_client() -> CatalogClient | None:
    return catalog_client
