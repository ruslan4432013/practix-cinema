"""Клиент сервиса Auth: сервисная учётка, выгрузка и точечный резолв пользователей.

Аутентификация — обычным логином сервисной учётки, а не подписанным нами
токеном. Разница принципиальная: сервис, который САМ выпускает себе admin-токен
общим секретом, обходит и роли, и денилист — то есть отзыв доступа для него
перестаёт работать. Учётка же заводится штатным ``practix_auth.cli``, и её можно
заблокировать так же, как любую другую.

Сюда ходят двое: команда синхронизации витрины (``iter_users``) и формирующий
воркер (``lookup_users``). Второй — на пути рассылки, и теория отдельно
предупреждает, что выгружать данные о пользователях нужно аккуратно, иначе
«можно нагрузить базу и другие компоненты сайта начнут тормозить». Поэтому
резолв идёт пачкой, ограничен ``NOTIFY_BUILDER_LOOKUP_BATCH`` и притормаживается
``NOTIFY_BUILDER_SLEEP`` (см. :mod:`practix_notifications.services.directory`).

## Два разных класса ошибок

``AuthUnavailable`` — «сейчас не получилось»: сеть, 5xx, 429, исчерпанные
попытки. Вызывающий обязан повторить позже, и пачка уезжает в retry-очередь.

``AuthClientError`` — «так и будет»: не тот пароль сервисной учётки, отобранная
роль, отсутствующая ручка. Повтор это не лечит, и пачке место в dead-letters, а
не в бесконечном круге по десять попыток.
"""

import logging
import time
import uuid
from typing import Any

import requests

from practix_core.backoff import compute_delay
from practix_core.context import get_request_id
from practix_notifications.core.config import settings

logger = logging.getLogger('notifications.auth_client')


class AuthClientError(Exception):
    """Ошибка, которую повтором не вылечить."""


class AuthUnavailable(AuthClientError):
    """Временная недоступность Auth. Вызывающий обязан повторить позже."""


class AuthClient:
    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = (base_url or settings.NOTIFY_AUTH_API_URL).rstrip('/')
        self._token: str | None = None

    def login(self) -> str:
        payload = {
            'login': settings.NOTIFY_AUTH_SERVICE_LOGIN,
            'password': settings.NOTIFY_AUTH_SERVICE_PASSWORD,
        }
        data = self._request('POST', '/api/v1/auth/login', json=payload)
        token = data.get('access_token')
        if not token:
            raise AuthClientError('Auth не вернул access_token')
        self._token = token
        return token

    def iter_users(self, page_size: int | None = None):
        """Все пользователи постранично. Останавливается по ``total``."""
        limit = page_size or settings.NOTIFY_SYNC_PAGE_SIZE
        offset = 0
        while True:
            data = self._request(
                'GET',
                '/api/v1/users',
                params={'limit': limit, 'offset': offset},
                authenticated=True,
            )
            items = data.get('items') or []
            if not items:
                return
            yield from items
            offset += len(items)
            if offset >= int(data.get('total') or 0):
                return

    def lookup_users(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        """Личные данные пачкой: ``{user_id: {...}}``.

        Ненайденные просто отсутствуют в словаре — Auth перечисляет их в
        ``missing``, и это не ошибка: пользователь мог быть удалён между
        синхронизацией витрины и рассылкой.
        """
        if not ids:
            return {}

        data = self._request('POST', '/api/v1/users/lookup', json={'ids': ids}, authenticated=True)
        missing = data.get('missing') or []
        if missing:
            logger.info('Auth does not know %d of %d requested users', len(missing), len(ids))

        # Элементы без `id` пропускаются, а не роняют разбор. `item['id']` дал бы
        # KeyError — исключение, которого нет в таксономии клиента: формирующий
        # воркер ловит только AuthUnavailable и AuthClientError, поэтому оно
        # добралось бы до общего обработчика консьюмера, стало бы RETRY и сожгло
        # весь бюджет сборки на ответе, который повтором не чинится.
        people: dict[str, dict[str, Any]] = {}
        for item in data.get('items') or []:
            user_id = item.get('id') if isinstance(item, dict) else None
            if not user_id:
                logger.warning('Auth returned a user without an id, skipping: %r', item)
                continue
            people[str(user_id)] = item
        return people

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        authenticated: bool = False,
    ) -> dict[str, Any]:
        url = f'{self._base_url}{path}'
        last_error: Exception | None = None

        for attempt in range(settings.NOTIFY_AUTH_MAX_ATTEMPTS):
            headers = self._headers(authenticated)
            try:
                response = requests.request(
                    method, url, json=json, params=params, headers=headers, timeout=settings.NOTIFY_AUTH_TIMEOUT
                )
            except requests.RequestException as exc:
                last_error = exc
                _sleep(attempt)
                continue

            if response.status_code == 401 and authenticated:
                # Токен протух — один раз переполучаем и повторяем.
                logger.info('Auth token expired, re-authenticating')
                self._token = None
                _sleep(attempt)
                continue
            if response.status_code == 429:
                # Auth ограничивает частоту по IP, и воркер рассылки в этот
                # лимит упирается штатно. Ждём столько, сколько попросили, но не
                # больше потолка: чужой (или сломанный) Retry-After не должен
                # парковать воркер на час.
                last_error = AuthUnavailable('Auth ответил 429')
                _sleep(attempt, retry_after=response.headers.get('Retry-After'))
                continue
            if response.status_code >= 500:
                last_error = AuthUnavailable(f'Auth ответил {response.status_code}')
                _sleep(attempt)
                continue
            if response.status_code >= 400:
                # Остальные 4xx повтором не лечатся: не тот пароль, нет прав, нет ручки.
                raise AuthClientError(f'Auth ответил {response.status_code}: {response.text[:300]}')
            return response.json()

        raise AuthUnavailable(f'Auth недоступен после {settings.NOTIFY_AUTH_MAX_ATTEMPTS} попыток: {last_error}')

    def _headers(self, authenticated: bool) -> dict[str, str]:
        # X-Request-Id ставится ВСЕГДА. У Auth RequestIdMiddleware работает в
        # режиме reject_400, а в воркере и в management-команде контекста запроса
        # нет и ContextVar пуст — без сгенерированного здесь идентификатора
        # каждый вызов возвращал бы 400 ещё до обработчика.
        headers = {
            'Content-Type': 'application/json',
            'X-Request-Id': get_request_id() or str(uuid.uuid4()),
        }
        if authenticated:
            headers['Authorization'] = f'Bearer {self._token or self.login()}'
        return headers


def _sleep(attempt: int, *, retry_after: str | None = None) -> None:
    delay = compute_delay(attempt, start=0.1, factor=2, border=5)
    if retry_after:
        try:
            delay = max(delay, min(float(retry_after), settings.NOTIFY_AUTH_RETRY_AFTER_MAX))
        except ValueError:
            # Retry-After умеет быть и датой по RFC 7231. Разбирать её здесь
            # незачем: обычного backoff достаточно.
            logger.debug('Unparsable Retry-After: %s', retry_after)
    time.sleep(delay)
