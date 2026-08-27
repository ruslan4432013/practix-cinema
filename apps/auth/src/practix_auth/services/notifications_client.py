"""Отчётное событие о регистрации для сервиса нотификаций.

Теория формулирует правило выбора контракта прямо: если из микросервиса уходит
«приказ», без которого функциональность не завершится, — это вызов HTTP API; если
нужно просто оповестить остальных, что у нас что-то произошло, — это отчётное
событие. Регистрация — второй случай: пользователь зарегистрирован независимо от
того, услышал ли кто-нибудь об этом.

Отсюда два свойства этого модуля:

* он никогда не бросает наружу. Провал означает «человек не получил
  приветственное письмо», а не «регистрация не удалась», и различать эти два
  исхода обязан код здесь, а не вызывающий;
* он вызывается ПОСЛЕ коммита и после формирования ответа, фоновой задачей.

Событие уходит HTTP-запросом в приём нотификаций, а не публикацией в брокер:
приём — центральный узел, он сам кладёт заявку в очередь. Auth не должен знать ни
про exchange, ни про routing key, ни вообще про то, что у нотификаций есть брокер.
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from practix_auth.core.config import settings
from practix_auth.models.entity import User
from practix_core.backoff import backoff_sleep
from practix_core.context import REQUEST_ID_HEADER

logger = logging.getLogger(__name__)

NOTIFY_EVENTS_PATH = '/api/v1/notifications/events'
EVENT_USER_REGISTERED = 'user.registered'


async def emit_user_registered(user: User, *, request_id: str | None = None) -> None:
    """Сообщить нотификациям о регистрации. Не бросает ни при каких условиях."""
    if not settings.NOTIFY_EVENTS_ENABLED:
        return

    payload = {
        'type': EVENT_USER_REGISTERED,
        'event_id': str(uuid.uuid4()),
        'occurred_at': datetime.now(UTC).isoformat(),
        'data': {'user_id': str(user.id), 'login': user.login, 'email': user.email},
    }
    try:
        await _post_with_backoff(payload, request_id=request_id)
    except Exception:  # noqa: BLE001 - и это осознанно: см. ниже
        # Ловится ВСЁ, включая программные ошибки. Регистрация уже закоммичена, и
        # ответ пользователю уже сформирован: любое исключение, вышедшее отсюда,
        # означало бы шум в фоновой задаче ради письма, а не ради регистрации.
        # Дедупликация на приёме идёт по user_id, поэтому потерянное событие можно
        # доставить повторно — синхронизацией подписчиков или запуском рассылки
        # руками.
        logger.warning('Событие user.registered не доставлено в нотификации', exc_info=True)


async def _post_with_backoff(payload: dict[str, Any], *, request_id: str | None) -> None:
    """Повторы только на 5xx и на сетевых ошибках.

    Цикл написан руками, потому что ``practix_core.backoff.retry`` — синхронный
    декоратор на ``time.sleep``: обёрнутая им корутина вернула бы объект корутины
    с первой попытки и не повторилась бы ни разу.

    Коды 2xx и 4xx одинаково означают «повторять бессмысленно», но по разным
    причинам: первое — доставлено (включая «дубликат» и «проигнорировано»),
    второе — наша ошибка в теле запроса, и её лечит правка кода, а не ретрай.
    """
    url = settings.NOTIFY_API_URL.rstrip('/') + NOTIFY_EVENTS_PATH
    headers = {'X-Internal-Token': settings.NOTIFY_INTAKE_TOKEN}
    if request_id:
        headers[REQUEST_ID_HEADER] = request_id

    attempts = max(1, settings.NOTIFY_EVENT_MAX_ATTEMPTS)
    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=settings.NOTIFY_EVENT_TIMEOUT) as client:
        for attempt in range(attempts):
            try:
                response = await client.post(url, json=payload, headers=headers)
                if response.status_code < 500:
                    if response.status_code >= 400:
                        logger.error('Нотификации отклонили событие: %s %s', response.status_code, response.text[:200])
                    return
                last_error = RuntimeError(f'notifications answered HTTP {response.status_code}')
            except httpx.HTTPError as exc:
                last_error = exc

            if attempt + 1 < attempts:
                await backoff_sleep(attempt, start=0.2, factor=2, border=2.0, jitter=0.2)

    raise last_error if last_error else RuntimeError('event was not delivered')
