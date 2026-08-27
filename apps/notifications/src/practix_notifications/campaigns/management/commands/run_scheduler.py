"""Планировщик: ``python -m practix_notifications.manage run_scheduler``.

Тот самый «генератор автоматических событий» из теории. Делает три вещи в ОДНОМ
потоке:

1. **тик расписаний** — что пора запустить (раз в ``NOTIFY_SCHEDULER_TICK_SECONDS``);
2. **слив outbox** — публикация того, что легло в базу вместе с бизнес-данными;
3. **веер** — разворачивание запущенной рассылки в получателей.

Однопоточность не упущение, а требование: канал pika не потокобезопасен, и
развести эти три задачи по потокам одного процесса — известный способ получить
``ChannelWrongStateError`` под нагрузкой. Понадобится масштабировать веер
отдельно — он выносится в свой контейнер из того же образа, менять код не придётся.

Пункты 2 и 3 крутятся каждую секунду, пункт 1 — по своему интервалу: отложенная
рассылка не обязана стартовать секунда в секунду, а вот пачка, уже лежащая в
очереди, ждать полминуты не должна.
"""

import logging
import signal
import time
from datetime import UTC, datetime
from typing import Any

from django.core.management.base import BaseCommand

from practix_notifications.broker import topology
from practix_notifications.broker.consumer import drain_queue
from practix_notifications.broker.publisher import BrokerConnection
from practix_notifications.core.config import settings
from practix_notifications.core.observability import init_observability
from practix_notifications.inbox import services as inbox
from practix_notifications.services import journal, outbox
from practix_notifications.services.fanout import handle_campaign_launched
from practix_notifications.services.tick import tick

logger = logging.getLogger('notifications.scheduler')

#: Период внутреннего цикла. Тик расписаний реже — по своим настройкам.
LOOP_INTERVAL_SECONDS = 1.0

#: Чистка ленты кабинета и журнала попыток. Раз в час: обе удаляют записи
#: месячной давности, и секунда разницы в моменте запуска не значит ничего, а
#: лишний DELETE в секунду на общей базе — значит.
PURGE_INTERVAL_SECONDS = 3600.0


class Command(BaseCommand):
    help = 'Запускает рассылки по расписанию, публикует outbox и разворачивает веер'

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument('--once', action='store_true', help='Один проход и выход (для отладки)')

    def handle(self, *args: Any, **options: Any) -> None:
        init_observability(instrument_django=False)

        stopping = False

        def _stop(signum: int, _frame: Any) -> None:
            nonlocal stopping
            logger.info('Signal %s received, stopping scheduler', signum)
            stopping = True

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        broker = BrokerConnection()
        last_tick = 0.0
        last_purge = 0.0
        logger.info('Scheduler started', extra={'tick_seconds': settings.NOTIFY_SCHEDULER_TICK_SECONDS})

        try:
            while not stopping:
                now = time.monotonic()
                if now - last_tick >= settings.NOTIFY_SCHEDULER_TICK_SECONDS:
                    last_tick = now
                    self._safe(lambda: tick(datetime.now(UTC)), 'schedule tick')

                if now - last_purge >= PURGE_INTERVAL_SECONDS:
                    last_purge = now
                    self._safe(
                        lambda: inbox.purge(older_than_days=settings.NOTIFY_INBOX_RETENTION_DAYS),
                        'inbox purge',
                    )
                    # Журнал попыток чистится тем же тиком: он растёт быстрее
                    # ленты (строка на КАЖДУЮ попытку, а не на доставленное
                    # письмо), а без срока хранения не чистился вовсе.
                    self._safe(
                        lambda: journal.purge_attempts(older_than_days=settings.NOTIFY_ATTEMPTS_RETENTION_DAYS),
                        'attempts purge',
                    )

                self._safe(lambda: outbox.drain(broker, limit=settings.NOTIFY_OUTBOX_BATCH), 'outbox drain')
                self._safe(
                    lambda: drain_queue(
                        broker,
                        queue=topology.QUEUE_FANOUT,
                        handler=lambda body, attempt: handle_campaign_launched(broker, body, attempt),
                        limit=settings.NOTIFY_OUTBOX_BATCH,
                    ),
                    'fan-out drain',
                )

                if options['once']:
                    break
                time.sleep(LOOP_INTERVAL_SECONDS)
        finally:
            broker.close()
            logger.info('Scheduler stopped')

    @staticmethod
    def _safe(action: Any, what: str) -> None:
        """Одна упавшая стадия не должна останавливать остальные две.

        Планировщик — единственный процесс, который двигает рассылки вперёд.
        Уронить его целиком из-за недоступного брокера значит остановить и тик
        расписаний, который к брокеру отношения не имеет.
        """
        try:
            action()
        except Exception:
            logger.exception('Scheduler stage failed: %s', what)
