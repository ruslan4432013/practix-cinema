"""Воркер сборки писем: ``python -m practix_notifications.manage run_builder``.

Забирает из ``notifications.build-email`` пачку идентификаторов, ходит за
личными данными в Auth, собирает персональные письма и публикует их в очередь
отправки. Всё, что связано с SMTP, живёт в ``run_worker``.

Почему это отдельный процесс, а не половина отправляющего:

* коннект к почтовому серверу стоит около пяти секунд и держится открытым — в
  нём нельзя простаивать, ожидая HTTP-ответ Auth;
* воркеры упираются в разные узкие места и масштабируются независимо: Auth
  лечится числом реплик сборки, медленный SMTP — числом реплик отправки;
* при отказе Auth можно погасить ТОЛЬКО сборку, и уже собранные письма
  продолжат уходить. Чек-лист теории требует ровно этого: «при отказе одной из
  подсистем нужно иметь возможность мгновенно отключить любые запросы на неё».

``Directory`` создаётся один на процесс: вместе с ним живут токен сервисной
учётки и кеш резолва, иначе каждая пачка начиналась бы с логина в Auth.
"""

import logging
import signal
from typing import Any

from django.core.management.base import BaseCommand

from practix_notifications.broker import topology
from practix_notifications.broker.consumer import consume_forever
from practix_notifications.broker.publisher import BrokerConnection
from practix_notifications.core.config import settings
from practix_notifications.core.observability import init_observability
from practix_notifications.services.building import handle_notification_requested
from practix_notifications.services.directory import Directory

logger = logging.getLogger('notifications.builder')


class Command(BaseCommand):
    help = 'Слушает очередь сборки, забирает данные пользователей из Auth и готовит письма'

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument('--queue', default=topology.QUEUE_BUILD_EMAIL, help='Очередь для чтения')

    def handle(self, *args: Any, **options: Any) -> None:
        # Django-инструментация воркеру не нужна: HTTP он не обслуживает.
        init_observability(instrument_django=False)

        stopping = False

        def _stop(signum: int, _frame: Any) -> None:
            nonlocal stopping
            logger.info('Signal %s received, finishing current batch', signum)
            stopping = True

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        broker = BrokerConnection()
        directory = Directory()
        logger.info('Builder started', extra={'queue': options['queue']})

        try:
            consume_forever(
                broker,
                queue=options['queue'],
                handler=lambda body, attempt: handle_notification_requested(broker, directory, body, attempt),
                stop=lambda: stopping,
                # Свой бюджет попыток: отказ Auth длиннее, чем икота почтового
                # сервера, и переживать его нужно дольше.
                max_attempts=settings.NOTIFY_BUILD_MAX_ATTEMPTS,
            )
        finally:
            broker.close()
            logger.info('Builder stopped')
