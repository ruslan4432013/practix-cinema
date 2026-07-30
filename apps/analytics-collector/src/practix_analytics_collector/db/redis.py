"""Подключения к Redis.

Клиентов ДВА, и это не дублирование, а следствие разной судьбы данных.

* ``redis`` — собственные данные сервиса: буфер деградации, счётчики rate limit
  и дедупликация по ``event_id``. Живут в отдельном экземпляре (compose:
  ``redis-ugc``), потому что многочасовой отказ Kafka наполняет буфер, и общая
  память с Auth означала бы, что авария в аналитике вытесняет сессии и роняет
  вход на сайт.
* ``auth_redis`` — денилист отозванных токенов. Его ведёт Auth-сервис, и читать
  его нужно там же, где он пишется. Отдельного клиента здесь раньше не было, и
  проверка ходила в базу коллектора (``UGC_REDIS_DB=1``), где ключей денилиста
  нет и не было: отозванный токен считался действительным.

Ни один из клиентов не обязателен для приёма события, поэтому оба настроены на
быстрый отказ (короткие таймауты), а все вызывающие стороны обрабатывают
недоступность Redis как штатную ситуацию.

Клиенты создаются в ``lifespan`` (как в ``rest/main.py``) — соединения нельзя
открывать до запуска event loop.
"""

from redis.asyncio import ConnectionPool, Redis

from practix_analytics_collector.core.config import settings

# Модульные синглтоны; инициализируются в lifespan, см. main.py.
redis: Redis | None = None
auth_redis: Redis | None = None


def get_redis() -> Redis | None:
    """Клиент собственных данных сервиса (буфер, rate limit, дедупликация).

    Сознательно возвращает ``None`` вместо исключения: вызывающий код обязан
    уметь работать без Redis.
    """
    return redis


def get_auth_redis() -> Redis | None:
    """Клиент денилиста токенов (Redis Auth-сервиса)."""
    return auth_redis


def _create(host: str, port: int, db: int, max_connections: int) -> Redis:
    pool = ConnectionPool(
        host=host,
        port=port,
        db=db,
        max_connections=max_connections,
        # Короткие таймауты: Redis на горячем пути ingest'а, зависшее
        # соединение не должно превращаться в зависший HTTP-запрос.
        socket_timeout=settings.UGC_REDIS_TIMEOUT,
        socket_connect_timeout=settings.UGC_REDIS_TIMEOUT,
        decode_responses=True,
    )
    return Redis(connection_pool=pool)


def create_redis() -> Redis:
    return _create(
        settings.ugc_redis_host,
        settings.ugc_redis_port,
        settings.UGC_REDIS_DB,
        settings.UGC_REDIS_MAX_CONNECTIONS,
    )


def create_auth_redis() -> Redis:
    # Пул меньше основного: сюда идёт одна операция на запрос с токеном, а не
    # три на каждое событие.
    return _create(
        settings.auth_redis_host,
        settings.auth_redis_port,
        settings.AUTH_REDIS_DB,
        max_connections=max(settings.UGC_REDIS_MAX_CONNECTIONS // 4, 8),
    )
