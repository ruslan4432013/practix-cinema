"""Ключи горячего слоя (ADR-004) и границы формы выдачи."""

import uuid

import pytest

from practix_recommendations_api.core.config import Settings
from practix_recommendations_api.services.cache import (
    POINTER_KEY,
    ShelfCache,
    decode,
    encode,
    personal_key,
    popular_key,
    similar_key,
)

FILM = uuid.UUID(int=1)
USER = uuid.UUID(int=2)


class FakeRedis:
    """Redis, которому можно приказать упасть или вернуть мусор."""

    def __init__(self, values: dict[str, str] | None = None, *, broken: bool = False):
        self.values = values or {}
        self.broken = broken
        self.writes: list[tuple[str, str, int | None]] = []
        self.reads = 0

    async def get(self, key: str):
        self.reads += 1
        if self.broken:
            raise ConnectionError('redis недоступен')
        return self.values.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        if self.broken:
            raise ConnectionError('redis недоступен')
        self.values[key] = value
        self.writes.append((key, value, ex))


def test_version_is_part_of_every_payload_key():
    """ADR-004: переключение версии — смена указателя, а не удаление ключей.

    Массовое DEL между старой и новой витриной означало бы, что весь трафик
    проваливается в PostgreSQL, — плановая операция сама себе создавала бы пик.
    С версией в ключе старое уходит по TTL, никого не разбудив.
    """
    assert similar_key(7, FILM).startswith('recs:v7:similar:')
    assert personal_key(7, USER).startswith('recs:v7:personal:')
    assert popular_key(7) == 'recs:v7:popular'
    assert similar_key(7, FILM) != similar_key(8, FILM)


def test_pointer_key_carries_no_version():
    """Иначе его нельзя было бы найти, не зная версию заранее."""
    assert POINTER_KEY == 'recs:pointer'


def test_encode_and_decode_round_trip():
    items = [(FILM, 0.75), (USER, 0.25)]
    assert decode(encode(items)) == items


def test_corrupt_cache_value_is_a_miss_not_a_crash():
    """В кэше может оказаться что угодно: чужой ключ, обрезанная запись, старый формат."""
    assert decode('не json') is None
    assert decode('{"unexpected": "shape"}') is None
    assert decode('[["не-uuid", 1.0]]') is None


async def test_cache_failure_reads_as_a_miss():
    """Отказ горячего слоя обязан означать «медленнее», а не «ошибка»."""
    cache = ShelfCache(FakeRedis(broken=True))

    assert await cache.get_list(popular_key(1), kind='popular') is None
    assert await cache.get_pointer() is None


async def test_cache_write_failure_does_not_propagate():
    cache = ShelfCache(FakeRedis(broken=True))

    await cache.put_list(popular_key(1), [(FILM, 1.0)])  # не должно бросить


async def test_empty_list_is_cached_deliberately():
    """Фильм без соседей иначе ходил бы в PostgreSQL на каждом запросе.

    По SLO таких фильмов до 40% каталога: промах по отсутствующему ключу
    неотличим от промаха по непрогретому, и без записи пустого значения
    деградация стоила бы обращения к базе каждый раз.
    """
    client = FakeRedis()
    cache = ShelfCache(client)

    await cache.put_list(similar_key(1, FILM), [])

    assert client.writes[0][1] == '[]'
    assert await cache.get_list(similar_key(1, FILM), kind='similar') == []


async def test_pointer_is_cached_in_process_between_reads():
    """Иначе каждый запрос платил бы round-trip ради числа, меняющегося раз в сутки."""
    client = FakeRedis({POINTER_KEY: '12'})
    cache = ShelfCache(client)

    assert await cache.get_pointer() == 12
    client.values[POINTER_KEY] = '13'
    assert await cache.get_pointer() == 12


async def test_absent_client_is_the_same_as_a_broken_one():
    """Redis не поднят — это вторая ступень лестницы, а не особый случай."""
    cache = ShelfCache(None)

    assert await cache.get_pointer() is None
    assert await cache.get_list(popular_key(1), kind='popular') is None
    await cache.put_list(popular_key(1), [(FILM, 1.0)])


def test_limit_is_clamped_not_rejected():
    """Завышенный limit — не повод отказать в рекомендациях.

    Вёрстка блока — дело фронта, а вот верхняя граница обязана быть: без неё
    один запрос вытянул бы всю персональную выдачу.
    """
    settings = Settings(RECS_DEFAULT_LIMIT=10, RECS_MAX_LIMIT=50)

    assert settings.clamp_limit(None) == 10
    assert settings.clamp_limit(5) == 5
    assert settings.clamp_limit(9999) == 50
    assert settings.clamp_limit(0) == 1


def test_auth_redis_falls_back_to_the_shared_redis_host():
    """Денилист ведёт Auth, и по умолчанию он там же, где остальной core Redis."""
    settings = Settings(REDIS_HOST='redis', REDIS_PORT=6379)

    assert settings.auth_redis_host == 'redis'
    assert settings.auth_redis_port == 6379

    overridden = Settings(REDIS_HOST='redis', AUTH_REDIS_HOST='auth-redis', AUTH_REDIS_PORT=6380)
    assert overridden.auth_redis_host == 'auth-redis'
    assert overridden.auth_redis_port == 6380


def test_shelf_redis_is_not_the_auth_redis():
    """Один клиент на оба назначения искал бы денилист не в той базе.

    Ровно на этом однажды обжёгся коллектор: отозванный токен считался
    действительным, потому что ключи денилиста лежали в чужой базе.
    """
    settings = Settings(REDIS_HOST='redis', RECS_REDIS_HOST='redis-recs')

    assert settings.auth_redis_host != settings.RECS_REDIS_HOST


def test_database_url_uses_the_async_driver():
    settings = Settings(RECS_POSTGRES_HOST='recs-db', RECS_POSTGRES_DB='recs_database')

    assert settings.database_url.startswith('postgresql+asyncpg://')
    assert settings.database_url.endswith('@recs-db:5432/recs_database')


def test_default_jwt_secret_is_reported_as_insecure():
    """Опасность значения по умолчанию в том, что оно работает."""
    assert Settings(AUTHJWT_SECRET_KEY='secret').insecure_defaults
    assert not Settings(AUTHJWT_SECRET_KEY='real-secret').insecure_defaults


def test_production_refuses_to_start_with_the_default_secret():
    with pytest.raises(ValueError, match='AUTHJWT_SECRET_KEY'):
        Settings(RECS_ENV='prod', AUTHJWT_SECRET_KEY='secret')


def test_the_whole_answer_fits_inside_the_nginx_read_timeout():
    """Регрессия: без этого ступень «источник недоступен → популярное» не срабатывает.

    Остановленный контейнер не отвечает отказом — его адрес исчезает из сети, и
    подключение висит до системного таймаута. Исключения при этом не возникает,
    ``except`` в лестнице деградации не выполняется, а ожидания Redis и
    PostgreSQL ещё и складываются. Запрос перекрывает ``proxy_read_timeout``
    nginx, и пользователь получает 504 вместо 200 с популярным. Поймано на
    живом стенде: погашенные recs-db и redis-recs давали 404 от nginx при
    формально исправном коде.

    3 секунды — ``proxy_read_timeout`` маршрута ^~ /api/v1/recommendations в
    infra/nginx/configs/site.conf. Поменяется там — обязан поменяться здесь.
    """
    nginx_proxy_read_timeout = 3.0
    settings = Settings()

    assert nginx_proxy_read_timeout > settings.RECS_REQUEST_BUDGET
    # Таймаут отдельного источника тоже обязан быть меньше общего срока: иначе
    # бюджет истекал бы раньше, чем источник успевал честно отказать, и в
    # метриках всё выглядело бы одинаково.
    assert settings.RECS_DB_TIMEOUT < settings.RECS_REQUEST_BUDGET
    assert settings.RECS_REDIS_TIMEOUT < settings.RECS_REQUEST_BUDGET


def test_every_outbound_dependency_is_bounded_in_time():
    """Ни один внешний вызов не имеет права висеть без ограничения.

    Их ровно четыре: витрина, горячий слой, денилист и каталог. Появление
    пятого без таймаута вернуло бы ту же поломку с другой стороны.
    """
    settings = Settings()

    assert settings.RECS_DB_TIMEOUT > 0
    assert settings.RECS_REDIS_TIMEOUT > 0
    assert settings.RECS_AUTH_REDIS_TIMEOUT > 0
    assert settings.RECS_MOVIES_API_TIMEOUT > 0


async def test_first_failure_blows_the_fuse_and_redis_is_left_alone():
    """Отказ ускорителя не имеет права отменять работу источника истины.

    Погашенный контейнер не отказывает, а молчит: клиент ждёт таймаут, и на
    один ответ таких ожиданий два (указатель и список). Вдвоём они съедали
    бюджет запроса раньше, чем очередь доходила до PostgreSQL, — выдача отдавала
    пустое популярное вместо честного чтения витрины. Поймано на живом стенде.
    """
    client = FakeRedis(broken=True)
    cache = ShelfCache(client)

    assert await cache.get_pointer() is None
    calls_after_first_failure = client.reads
    assert await cache.get_list(popular_key(1), kind='popular') is None

    assert client.reads == calls_after_first_failure, 'после первой неудачи в Redis ходить нельзя'


async def test_the_fuse_lets_go_after_its_time():
    client = FakeRedis({POINTER_KEY: '7'}, broken=True)
    cache = ShelfCache(client)

    await cache.get_pointer()
    client.broken = False
    # Пережигаем предохранитель вручную: ждать в юнит-тесте нечего.
    cache._fuse_until = 0.0

    assert await cache.get_pointer() == 7


async def test_a_blown_fuse_forgets_the_remembered_version():
    """Иначе выдача читала бы строки версии, которой в базе уже нет.

    Пока кэш погашен, номер версии берётся из базы; подсунуть в этот момент
    запомненное число значило бы отдать пустой список от несуществующей версии.
    """
    client = FakeRedis({POINTER_KEY: '5'})
    cache = ShelfCache(client)
    assert await cache.get_pointer() == 5

    client.broken = True
    await cache.get_list(popular_key(5), kind='popular')

    assert await cache.get_pointer() is None
