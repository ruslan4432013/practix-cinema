"""Единственный клиент Redis в этом сервисе — и он ЧУЖОЙ.

Своих данных в Redis у нотификаций нет: очередь — это RabbitMQ, состояние — это
Postgres. Здесь читается денилист отозванных токенов, который ведёт Auth, а
читать его нужно там же, где он пишется. Собственный экземпляр (или другой номер
базы) означал бы, что отозванный токен считается действующим, — та самая ошибка,
которую уже чинили в коллекторе, где по этой причине держат ДВА клиента.

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
