"""Отчётное событие «в кинотеатре появился новый фильм».

Теория перечисляет источники уведомлений и прямо называет этот: «Серию может
опубликовать модератор, нажав на кнопку (событие)». Кнопка здесь — сохранение
фильма в админке, и всё, что она делает дополнительно, — это HTTP-запрос в приём
нотификаций. Ни очереди, ни списка получателей, ни письма админка не видит:
разворачивать аудиторию внутри запроса на сохранение формы — ровно то, от чего
теория предостерегает («система будет очень долго собирать данные для уведомления
тысячи пользователей»).

Три условия, каждое из которых отсекает свой ложный вызов:

* ``created=False`` — правка описания или рейтинга существующего фильма. Новым он
  от этого не становится;
* ``raw=True`` — загрузка фикстур ``loaddata``. Фикстура не событие;
* выключенный флаг — стенд без профиля ``notifications``, где такого хоста нет.

Массовой пальбы при заливке каталога не будет и без флага: ``database_dump.sql`` и
``seed_db.py`` пишут сырым psycopg, а ``bulk_create`` не шлёт ``post_save``.
"""

import logging
import threading
import uuid
from datetime import UTC, datetime

from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from example.http_retry import request_with_retry

from movies.models import FilmWork

logger = logging.getLogger(__name__)

NOTIFY_EVENTS_PATH = '/api/v1/notifications/events'
EVENT_FILM_PUBLISHED = 'film.published'


@receiver(post_save, sender=FilmWork, dispatch_uid='movies.film_published')
def film_published(sender, instance: FilmWork, created: bool, raw: bool = False, **kwargs) -> None:
    if raw or not created or not getattr(settings, 'NOTIFY_EVENTS_ENABLED', False):
        return

    payload = {
        'type': EVENT_FILM_PUBLISHED,
        'event_id': str(uuid.uuid4()),
        'occurred_at': datetime.now(UTC).isoformat(),
        'data': {
            'film_id': str(instance.id),
            'title': instance.title,
            'year': instance.creation_date.year if instance.creation_date else None,
        },
    }

    # on_commit: письмо о фильме, чья транзакция откатилась, — это письмо о том,
    # чего нет. Поток поверх этого: отправка не должна держать форму сохранения
    # открытой, пока нотификации отвечают. enable-threads в uwsgi.ini включён —
    # без него поток молча не запустился бы.
    transaction.on_commit(
        lambda: threading.Thread(target=_post_event, args=(payload,), name='film-published', daemon=True).start()
    )


def _post_event(payload: dict) -> None:
    """Отправить событие. Не бросает: каталог живёт без нотификаций."""
    url = settings.NOTIFY_API_URL.rstrip('/') + NOTIFY_EVENTS_PATH
    # Под try — ВЕСЬ разбор ответа, а не только сам запрос. «Не бросает» здесь
    # обещание, а не пожелание: вызывает эту функцию поток, у которого нет
    # никакого обработчика выше, и любое исключение отсюда просто исчезает,
    # напечатав трейсбек в stderr. Оставленная снаружи проверка кода ответа
    # делала обещание неверным для всякого ответа, который не оказался
    # настоящим `requests.Response`.
    try:
        response = request_with_retry(
            'POST',
            url,
            attempts=settings.NOTIFY_EVENT_MAX_ATTEMPTS,
            timeout=settings.NOTIFY_EVENT_TIMEOUT,
            json=payload,
            headers={'X-Internal-Token': settings.NOTIFY_INTAKE_TOKEN},
        )
        if response.status_code >= 400:
            # Повтор бессмысленен: 4xx — наша ошибка в теле, 5xx уже переспрошен.
            # Дедупликация на приёме идёт по film_id, поэтому анонс можно запустить
            # руками из панели рассылок, не рискуя вторым письмом.
            logger.error('Нотификации отклонили событие: %s %s', response.status_code, response.text[:200])
    except Exception:  # noqa: BLE001 - осознанно: это фоновый поток
        # Исключение, вышедшее из потока, ничего не чинит и никем не будет
        # поймано, а каталог обязан работать без сервиса нотификаций.
        logger.warning('Событие film.published не доставлено в нотификации', exc_info=True)
