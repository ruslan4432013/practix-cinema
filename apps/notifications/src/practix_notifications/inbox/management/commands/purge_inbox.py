"""Ручная чистка ленты кабинета.

Планировщик делает то же самое раз в час; отдельная команда нужна для разового
прогона после смены срока хранения и для стенда, где планировщик не поднят.
"""

from typing import Any

from django.core.management.base import BaseCommand

from practix_notifications.core.config import settings
from practix_notifications.inbox import services as inbox


class Command(BaseCommand):
    help = 'Удаляет записи ленты уведомлений старше срока хранения'

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            '--days',
            type=int,
            default=settings.NOTIFY_INBOX_RETENTION_DAYS,
            help='Срок хранения в днях (по умолчанию NOTIFY_INBOX_RETENTION_DAYS)',
        )

    def handle(self, *args: Any, **options: Any) -> None:
        deleted = inbox.purge(older_than_days=options['days'])
        self.stdout.write(f'Удалено записей ленты: {deleted}')
