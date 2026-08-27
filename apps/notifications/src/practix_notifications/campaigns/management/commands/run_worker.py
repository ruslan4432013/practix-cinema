"""Воркер отправки: ``python -m practix_notifications.manage run_worker``.

Отдельный процесс, а не поток внутри панели, — по той же причине, по которой
теория выносит отправку из API: «отправку уведомления нельзя реализовать в
реальном времени: она должна происходить в отдельном от API сервиса процессе».
Масштабируется репликами контейнера: очередь одна, консьюмеров сколько нужно.

Сообщение приезжает сюда УЖЕ СОБРАННЫМ — его сделал ``run_builder``. Здесь
только доставка, и это главная причина разделения: соединение с почтовым
сервером стоит около пяти секунд и держится открытым весь срок жизни процесса,
поэтому простаивать в нём, ожидая ответ Auth, нельзя.

Каналов у одной пачки может быть несколько, поэтому отправитель выбирается ПО
КАНАЛУ ЗАДАЧИ (``SenderPool``), а не по флагу запуска: ``--channels`` теперь
задаёт, что этот процесс берётся обрабатывать, и нужен только для того, чтобы
отдельный контейнер на канал остался возможен без правки кода.

SIGTERM обрабатывается: ``docker compose stop`` даёт процессу закончить текущую
пачку, а не обрывает его посреди отправки — иначе неподтверждённая пачка
переотправится целиком (безвредно благодаря идемпотентности, но бессмысленно).
"""

import logging
import signal
from typing import Any

from django.core.management.base import BaseCommand

from practix_notifications.broker import topology
from practix_notifications.broker.consumer import consume_forever
from practix_notifications.broker.publisher import BrokerConnection
from practix_notifications.channels import IMPLEMENTED, SenderPool
from practix_notifications.core.config import settings
from practix_notifications.core.observability import init_observability
from practix_notifications.services.delivery import handle_notification_prepared

logger = logging.getLogger('notifications.worker')


class Command(BaseCommand):
    help = 'Слушает очередь уведомлений и отправляет сообщения'

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            '--channels',
            default=','.join(sorted(IMPLEMENTED)),
            help='Каналы через запятую, которые обслуживает этот воркер',
        )
        parser.add_argument('--queue', default=topology.QUEUE_SEND_EMAIL, help='Очередь для чтения')

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
        channels = {item.strip() for item in options['channels'].split(',') if item.strip()}
        logger.info('Worker started', extra={'channels': sorted(channels), 'queue': options['queue']})

        try:
            # Соединение брокера отдаётся пулу: websocket-отправитель публикует
            # push'и в него же, и вторая AMQP-сессия на процесс не заводится.
            with SenderPool(allowed=channels, broker=broker) as senders:
                consume_forever(
                    broker,
                    queue=options['queue'],
                    handler=lambda body, attempt: handle_notification_prepared(broker, senders, body, attempt),
                    stop=lambda: stopping,
                    max_attempts=settings.NOTIFY_MAX_ATTEMPTS,
                )
        finally:
            broker.close()
            logger.info('Worker stopped')
