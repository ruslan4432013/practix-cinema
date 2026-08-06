"""Ожидание готовности сервиса, у которого есть HTTP-проба.

Вынесено из ``wait_for_rabbitmq`` и ``wait_for_mailpit``: обе проверки — это
«дёрнуть URL и повторить, пока не ответит 200», и различаются они только адресом
и наличием basic-аутентификации. Держать две копии одного цикла ретраев смысла
нет, а jscpd в этом репозитории — жёсткий гейт.

``urllib`` вместо ``requests``: скрипты запускаются перед установкой тестовых
зависимостей и не должны тянуть ничего сверх stdlib.
"""

import urllib.error
import urllib.request

# См. комментарий в wait_for_es.py — скрипты запускаются файлом.
from practix_testing.utils.retry_utils import backoff


@backoff(max_attempts=60)
def wait_for_http(url: str, *, service: str, auth: tuple[str, str] | None = None, timeout: float = 3.0) -> None:
    """Дождаться ответа 200 по адресу. Бросает ``ConnectionError`` до последней попытки."""
    opener = urllib.request.build_opener()
    if auth is not None:
        manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        manager.add_password(None, url, auth[0], auth[1])
        opener = urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler(manager))

    try:
        with opener.open(url, timeout=timeout) as response:
            if response.status != 200:
                raise ConnectionError(f'{service} returned {response.status}')
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ConnectionError(f'{service} is not available: {exc}') from exc
