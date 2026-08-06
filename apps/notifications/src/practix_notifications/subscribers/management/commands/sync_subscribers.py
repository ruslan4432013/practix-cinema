"""Синхронизация витрины подписчиков из Auth.

Запускается по расписанию снаружи (cron, ручной вызов) — сознательно, а не тиком
планировщика: выгрузка пользователей нагружает чужой сервис, и решение, как часто
это делать, принадлежит эксплуатации, а не коду.

Локальные настройки уведомлений (таймзона, отписки) синхронизация НЕ трогает: они
принадлежат нам, в Auth их нет, и перезаписать их выгрузкой значило бы вернуть
пользователю письма, от которых он отписался.
"""

import logging
from datetime import UTC, datetime
from typing import Any

from django.core.management.base import BaseCommand

from practix_notifications.core.config import settings
from practix_notifications.services.auth_client import AuthClient, AuthClientError
from practix_notifications.subscribers.models import Subscriber

logger = logging.getLogger('notifications.sync')


class Command(BaseCommand):
    help = 'Обновляет локальную витрину подписчиков из сервиса Auth'

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument('--page-size', type=int, default=settings.NOTIFY_SYNC_PAGE_SIZE)

    def handle(self, *args: Any, **options: Any) -> None:
        client = AuthClient()
        created = updated = skipped = 0
        now = datetime.now(UTC)

        try:
            for item in client.iter_users(page_size=options['page_size']):
                outcome = self._upsert(item, now)
                created += outcome == 'created'
                updated += outcome == 'updated'
                skipped += outcome == 'skipped'
        except AuthClientError as exc:
            # Не роняем команду трейсбеком: её запускает cron, и в логе должна
            # быть причина, а не стек вызовов requests.
            raise SystemExit(f'Синхронизация не удалась: {exc}') from exc

        self.stdout.write(f'Создано: {created}, обновлено: {updated}, пропущено (устаревшие данные): {skipped}')

    @staticmethod
    def _upsert(item: dict, now: datetime) -> str:
        source_version = _parse_dt(item.get('created_at'))
        existing = Subscriber.objects.filter(pk=item['id']).first()

        if existing is None:
            Subscriber.objects.create(
                id=item['id'],
                login=item.get('login') or '',
                email=item.get('email') or '',
                timezone=settings.NOTIFY_DEFAULT_TIMEZONE,
                source_version=source_version,
                synced_at=now,
            )
            return 'created'

        # Две страницы выгрузки теоретически могут прийти в обратном порядке —
        # теория RabbitMQ советует ровно такое поле, чтобы не откатить свежие
        # данные старыми.
        if existing.source_version and source_version and source_version < existing.source_version:
            return 'skipped'

        existing.login = item.get('login') or existing.login
        existing.email = item.get('email') or existing.email
        existing.source_version = source_version or existing.source_version
        existing.synced_at = now
        existing.save(update_fields=['login', 'email', 'source_version', 'synced_at', 'updated_at'])
        return 'updated'


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
