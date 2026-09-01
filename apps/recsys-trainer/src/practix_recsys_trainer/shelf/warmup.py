"""Прогрев горячего слоя после переключения указателя.

Порядок операций здесь — половина смысла ADR-004. Указатель в Redis
выставляется ПОСЛЕДНИМ, уже после того как разложены сами списки: иначе выдача,
увидев новый номер версии, пошла бы за ключами, которых ещё нет, и первые
секунды после переключения весь трафик проваливался бы в PostgreSQL — то есть
плановая операция сама себе создавала бы пик.

Старые ключи не удаляются вовсе. Они уходят по TTL, никого не разбудив, и
именно поэтому версия входит в ключ: массовое ``DEL`` между старой и новой
витриной — это тот же самый пик, только устроенный вручную.

Прогревается не всё, а верхушка: хвост каталога никто не запрашивает, а
``volatile-lru`` всё равно выселит непрошенное. Персональные ключи не
прогреваются совсем — их сотни тысяч, и попадание в конкретного пользователя
предсказать нельзя; они наполняются по промахам.

ОТКАЗ REDIS ЗДЕСЬ НЕ ОТМЕНЯЕТ ОБУЧЕНИЕ. Витрина уже в PostgreSQL и уже
опубликована; непрогретый кэш означает «первые запросы медленнее», а не
«обучение не состоялось».
"""

import json
import logging

import redis

from practix_recsys_trainer.core.config import settings

logger = logging.getLogger(__name__)

POINTER_KEY = 'recs:pointer'


def _encode(items: list[tuple[str, float]]) -> str:
    """Тот же формат, что читает ``recommendations-api``: список пар."""
    return json.dumps([[film_id, score] for film_id, score in items])


def warm(
    version: int,
    *,
    popular: list[tuple[str, float]],
    similar: dict[str, list[tuple[str, float]]],
) -> bool:
    """Раскладывает списки и в конце переставляет указатель. ``False`` — Redis не ответил."""
    client = redis.Redis(
        host=settings.RECS_REDIS_HOST,
        port=settings.RECS_REDIS_PORT,
        db=settings.RECS_REDIS_DB,
        socket_timeout=settings.RECS_REDIS_TIMEOUT,
        socket_connect_timeout=settings.RECS_REDIS_TIMEOUT,
        decode_responses=True,
    )
    try:
        pipe = client.pipeline(transaction=False)
        pipe.set(f'recs:v{version}:popular', _encode(popular), ex=settings.RECS_CACHE_TTL)

        warmed = 0
        for film_id, neighbours in similar.items():
            if warmed >= settings.RECS_TRAINER_WARMUP_FILMS:
                break
            pipe.set(f'recs:v{version}:similar:{film_id}', _encode(neighbours), ex=settings.RECS_CACHE_TTL)
            warmed += 1

        pipe.execute()
        # Указатель — последним и БЕЗ TTL: он должен пережить любой список.
        # Истёкший указатель означал бы поход в базу за версией на каждом
        # запросе, то есть ровно ту нагрузку, от которой кэш и защищает.
        client.set(POINTER_KEY, str(version))
        logger.info('Горячий слой прогрет: версия %s, фильмов %s', version, warmed)
        return True
    except Exception as exc:  # noqa: BLE001 — прогрев не имеет права отменить публикацию
        logger.warning('WARMUP FAILED: витрина опубликована, но кэш не прогрет (%s)', exc)
        return False
    finally:
        client.close()
