"""Клиент сервиса сокращения ссылок.

Один вызов: «дай короткую ссылку, ведущую туда-то». Аутентификация — общим
служебным токеном, а не учёткой: у шортенера нет ролей и нет пользователей,
заводить их ради одного вызова значило бы держать второй механизм авторизации.

## Два разных класса ошибок

Деление то же, что у ``auth_client``, и по той же причине — от него зависит,
уедет пачка в парковочную очередь или в dead-letters.

``ShortenerUnavailable`` — «сейчас не получилось»: сеть, 5xx, 429, исчерпанные
попытки. Пачка ждёт и повторяется.

``ShortenerClientError`` — «так и будет»: не тот токен, целевой адрес не прошёл
проверку белого списка, нет ручки. Повтор это не лечит.
"""

import logging
import time
import uuid
from typing import Any

import requests

from practix_core.backoff import compute_delay
from practix_core.context import get_request_id
from practix_notifications.core.config import settings

logger = logging.getLogger('notifications.shortener_client')


class ShortenerClientError(Exception):
    """Ошибка, которую повтором не вылечить."""


class ShortenerUnavailable(ShortenerClientError):
    """Временная недоступность сервиса ссылок. Вызывающий обязан повторить позже."""


class ShortenerClient:
    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = (base_url or settings.NOTIFY_SHORTENER_API_URL).rstrip('/')

    def create_link(
        self,
        *,
        target_url: str,
        kind: str,
        user_id: str | None = None,
        ttl_hours: int | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Короткая ссылка. Возвращает готовый внешний адрес для письма."""
        payload: dict[str, Any] = {'target_url': target_url, 'kind': kind}
        if user_id:
            payload['user_id'] = user_id
        if ttl_hours:
            payload['ttl_hours'] = ttl_hours
        if idempotency_key:
            payload['idempotency_key'] = idempotency_key

        data = self._post('/api/v1/links', payload)
        short_url = data.get('short_url')
        if not short_url:
            raise ShortenerClientError('сервис ссылок не вернул short_url')
        return str(short_url)

    def _post(self, path: str, payload: dict) -> dict[str, Any]:
        url = f'{self._base_url}{path}'
        last_error: Exception | None = None

        for attempt in range(settings.NOTIFY_SHORTENER_MAX_ATTEMPTS):
            try:
                response = requests.post(
                    url, json=payload, headers=self._headers(), timeout=settings.NOTIFY_SHORTENER_TIMEOUT
                )
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(compute_delay(attempt, start=0.1, factor=2, border=5))
                continue

            if response.status_code == 429 or response.status_code >= 500:
                last_error = ShortenerUnavailable(f'сервис ссылок ответил {response.status_code}')
                time.sleep(compute_delay(attempt, start=0.1, factor=2, border=5))
                continue
            if response.status_code >= 400:
                # Не тот токен, целевой адрес вне белого списка, нет ручки.
                raise ShortenerClientError(f'сервис ссылок ответил {response.status_code}: {response.text[:300]}')
            return response.json()

        raise ShortenerUnavailable(
            f'сервис ссылок недоступен после {settings.NOTIFY_SHORTENER_MAX_ATTEMPTS} попыток: {last_error}'
        )

    @staticmethod
    def _headers() -> dict[str, str]:
        # X-Request-Id ставится ВСЕГДА: в воркере контекста запроса нет и
        # ContextVar пуст, а сгенерированный здесь идентификатор связывает
        # запись в логе воркера с записью в логе шортенера.
        return {
            'Content-Type': 'application/json',
            'X-Request-Id': get_request_id() or str(uuid.uuid4()),
            'Authorization': f'Bearer {settings.NOTIFY_SHORTENER_INTERNAL_TOKEN}',
        }
