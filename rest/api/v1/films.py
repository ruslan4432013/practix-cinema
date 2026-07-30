from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from redis.asyncio import Redis

from api.v1.dependencies import (
    PaginationParams,
    ensure_subscription_access,
    get_auth_context,
    get_user_roles,
)
from db.redis_db import get_redis
from models.film import FilmDetail, FilmGenre, FilmPerson, FilmShort
from services.film import FilmService, get_film_service

router = APIRouter()


@router.get(
    '/search',
    response_model=list[FilmShort],
    summary='Полнотекстовый поиск фильмов',
    description='Ищет фильмы по названию и описанию. Поддерживает пагинацию.',
    response_description='Список фильмов с названием и рейтингом',
    tags=['Фильмы'],
    responses={
        status.HTTP_401_UNAUTHORIZED: {'description': 'Неверный логин или пароль'},
    },
)
async def films_search(
    query: str = Query(..., description='Поисковый запрос'),
    pagination: PaginationParams = Depends(PaginationParams),
    film_service: FilmService = Depends(get_film_service),
    roles: list[str] = Depends(get_user_roles),
) -> list[FilmShort]:
    films = await film_service.search(query, pagination.page_number, pagination.page_size, roles)
    return [FilmShort(uuid=f.id, title=f.title, imdb_rating=f.rating) for f in films]


@router.get(
    '',
    response_model=list[FilmShort],
    summary='Список фильмов',
    description='Возвращает список фильмов с сортировкой и фильтрацией по жанру. Сортировка: `-imdb_rating` (убывание), `imdb_rating` (возрастание).',
    response_description='Список фильмов с названием и рейтингом',
    tags=['Фильмы'],
    responses={
        status.HTTP_401_UNAUTHORIZED: {'description': 'Неверный логин или пароль'},
    },
)
async def films_list(
    sort: str | None = Query(None, description='Поле сортировки, например -imdb_rating'),
    genre: UUID | None = Query(None, description='Фильтр по UUID жанра'),
    pagination: PaginationParams = Depends(PaginationParams),
    film_service: FilmService = Depends(get_film_service),
    roles: list[str] = Depends(get_user_roles),
) -> list[FilmShort]:
    films = await film_service.get_list(
        sort=sort,
        genre=str(genre) if genre else None,
        page_number=pagination.page_number,
        page_size=pagination.page_size,
        roles=roles,
    )
    return [FilmShort(uuid=f.id, title=f.title, imdb_rating=f.rating) for f in films]


@router.get(
    '/{film_id}',
    response_model=FilmDetail,
    summary='Детальная информация о фильме',
    description='Возвращает полную карточку фильма: описание, жанры, актёров, режиссёров и сценаристов.',
    response_description='Детальная карточка фильма',
    tags=['Фильмы'],
    responses={
        status.HTTP_404_NOT_FOUND: {'description': 'Фильм не найден'},
        status.HTTP_403_FORBIDDEN: {'description': 'Требуется подписка для доступа к контенту'},
        status.HTTP_401_UNAUTHORIZED: {'description': 'Неверный логин или пароль'},
    },
)
async def film_details(
    film_id: UUID,
    film_service: FilmService = Depends(get_film_service),
    context: dict = Depends(get_auth_context),
    redis: Redis = Depends(get_redis),
) -> FilmDetail:
    film = await film_service.get_by_id(str(film_id))
    if not film:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='film not found')

    # Публичный контент доступен всем; контент по подписке требует авторизации
    # и актуального статуса подписки (с изящной деградацией при падении Auth).
    access_type = getattr(film, 'access_type', 'public')
    if access_type == 'subscribers':
        await ensure_subscription_access(context, redis)

    return FilmDetail(
        uuid=film.id,
        title=film.title,
        imdb_rating=film.rating,
        description=film.description,
        genre=[FilmGenre(uuid=g.id, name=g.name) for g in film.genres],
        actors=[FilmPerson(uuid=p.id, full_name=p.full_name) for p in film.actors],
        writers=[FilmPerson(uuid=p.id, full_name=p.full_name) for p in film.writers],
        directors=[FilmPerson(uuid=p.id, full_name=p.full_name) for p in film.directors],
    )
