"""Единственный клиент Redis в этом сервисе — и экземпляр за ним ОБЩИЙ.

Данные сервиса живут не здесь: очередь — это RabbitMQ, состояние — это Postgres.
Redis используется ровно для двух вещей, и обе не про хранение.

Первая — денилист отозванных токенов, который ведёт Auth. Читать его нужно там
же, где он пишется: собственный экземпляр (или другой номер базы) означал бы, что
отозванный токен считается действующим, — та самая ошибка, которую уже чинили в
коллекторе, где по этой причине держат ДВА клиента.

Вторая — слот темпа отправки писем (``channels/pacing.py``). Это уже СВОЙ ключ в
чужом экземпляре, и заводить ради одного волатильного ключа отдельный контейнер
незачем: точно так же websocket-шлюз хранит здесь свои одноразовые ticket'ы, и
режим ``volatile-lru`` этого Redis подходит обоим.

Клиент синхронный: сервис целиком синхронный (pika на BlockingConnection,
smtplib, Django ORM), и асинхронный клиент здесь пришлось бы крутить в своём
event loop ради одного ``GET``.
"""

import logging

import redis

from practix_notifications.core.config import settings

logger = logging.getLogger('notifications.redis')

_client: redis.Redis | None = None


def get_auth_redis() -> redis.Redis:
    """Клиент Redis сервиса Auth. Создаётся лениво и переиспользуется.

    Ленивый синглтон, а не создание на запрос: пул соединений внутри клиента и
    существует ради переиспользования, а новый клиент на каждую проверку токена
    свёл бы его на нет.
    """
    global _client
    if _client is None:
        _client = redis.Redis(
            host=settings.AUTH_REDIS_HOST,
            port=settings.AUTH_REDIS_PORT,
            db=settings.AUTH_REDIS_DB,
            # Таймауты обязательны и малы: проверка токена стоит на пути
            # пользовательского запроса, и залипший Redis не должен превращаться
            # в залипший gunicorn-воркер.
            socket_timeout=settings.NOTIFY_REDIS_TIMEOUT,
            socket_connect_timeout=settings.NOTIFY_REDIS_TIMEOUT,
            decode_responses=True,
        )
    return _client


def reset_for_testing() -> None:
    """Сбросить синглтон между тестами, меняющими настройки."""
    global _client
    _client = None
