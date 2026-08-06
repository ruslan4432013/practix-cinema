"""Ссылка отписки.

Требование задания — «должна быть возможность настройки уведомлений
пользователем, в том числе отключение уведомлений». Настройки живут в
``ChannelOptout``, а эта ссылка — способ дотянуться до них из письма, не заводя
пользователю пароль от панели.

Подпись, а не идентификатор в открытом виде: иначе подставив чужой UUID можно
отписать кого угодно. ``TimestampSigner`` даёт и подпись, и срок жизни, и не
требует таблицы токенов.
"""

from django.core import signing

from practix_notifications.core.config import settings

SALT = 'practix.notifications.unsubscribe'


def make_token(subscriber_id: str, channel: str, category: str) -> str:
    return signing.dumps({'s': str(subscriber_id), 'ch': channel, 'cat': category}, salt=SALT)


def read_token(token: str) -> dict[str, str]:
    """Разобрать токен. Бросает ``signing.BadSignature`` на подделку и истечение."""
    max_age = settings.NOTIFY_UNSUBSCRIBE_TTL_DAYS * 24 * 3600
    return signing.loads(token, salt=SALT, max_age=max_age)


def unsubscribe_url(subscriber_id: str, channel: str, category: str) -> str:
    base = settings.NOTIFY_PUBLIC_BASE_URL.rstrip('/')
    return f'{base}/api/v1/notifications/unsubscribe/{make_token(subscriber_id, channel, category)}'
