"""HTTP-запрос с ограниченными ретраями — общий для исходящих вызовов сервиса.

Вынесено из ``users/auth_backend.py``, когда потребителей стало двое: там же
остался бы второй экземпляр той же арифметики задержек, а это ровно тот клон,
который ловит порог дублирования в CI.

Почему не ``practix_core.backoff``: этот сервис живёт ВНЕ uv-workspace (Python
3.12, свой ``uv.lock``, потому что uwsgi собирается из исходников), и общего
первопартийного кода с workspace'ом у него нет — импортировать оттуда физически
нечего. Три существующих форка (middleware, инициализация Sentry, JSON-логгер)
описаны в ``docs/monorepo.md``; четвёртым этот модуль не становится, потому что
не копирует библиотечный код, а собирает вместе то, что уже было здесь.
"""

import logging
import time

import requests

logger = logging.getLogger(__name__)


def request_with_retry(
    method: str,
    url: str,
    *,
    attempts: int = 3,
    timeout: float = 2.0,
    start_delay: float = 0.1,
    factor: float = 2.0,
    **kwargs,
) -> requests.Response:
    """Выполнить запрос, повторяя его при сетевых ошибках.

    Повторяются только ``RequestException`` — то есть «не дозвонились». Ответ с
    любым кодом возвращается как есть: решать, что делать с 4xx или 5xx, должен
    вызывающий, у которого есть контекст.
    """
    delay = start_delay
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return requests.request(method, url, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            last_exc = exc
            logger.warning('Запрос %s %s не удался (%s), попытка %d', method, url, exc, attempt + 1)
            if attempt + 1 < attempts:
                time.sleep(delay)
                delay *= factor
    assert last_exc is not None
    raise last_exc
