"""Клиент Auth: единственный вызов — «пометь этот адрес подтверждённым».

Флагом владеет Auth, потому что Auth владеет пользователем: писать в его базу
напрямую значило бы завести второго хозяина у одной колонки. Отсюда HTTP.

Почему подтверждает шортенер, а не сам Auth по своей ссылке. Альтернатива —
сделать целевым адресом ручку Auth и отдать редирект ей — даёт ДВА независимых
источника срока годности: строку здесь и токен там. Расходятся они молча, а
проявляется расхождение ссылкой, которая открывается, но ничего не
подтверждает. Здесь срок один — колонка ``expires_at``.

Аутентификация — обычной служебной учёткой (``practix_auth.cli
create-service-account``), а не самоподписанным токеном: сервис, который сам
себе выписывает admin-токен общим секретом, обходит и роли, и денилист, то есть
отозвать ему доступ нечем. Роль узкая — ``email-confirmer``.

Клиент асинхронный, в отличие от синхронного клиента нотификаций: тот живёт в
воркере, а этот — на пути пользовательского запроса, и ``requests`` заблокировал
бы event loop вместе со всеми параллельными переходами.
"""

import asyncio
import logging
import uuid

import httpx

from practix_core.backoff import compute_delay
from practix_core.context import get_request_id
from practix_link_shortener.core.config import settings
from practix_link_shortener.services.exceptions import (
    ConfirmMisconfigured,
    ConfirmRejected,
    ConfirmUnavailable,
)

logger = logging.getLogger(__name__)


class AuthClient:
    """Тонкий клиент Auth. Держит один access-токен служебной учётки."""

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = (base_url or settings.SHORTENER_AUTH_API_URL).rstrip('/')
        self._token: str | None = None
        # Замок вокруг получения токена. Клиент один на процесс, а переходы по
        # ссылкам идут параллельно: без замка в момент протухания каждая
        # корутина увидела бы `self._token` пустым и ушла бы логиниться сама —
        # пачка одновременных /auth/login ровно тогда, когда Auth и так под
        # нагрузкой (и лишние записи в `user:{id}:sessions` на каждый вход).
        self._token_lock = asyncio.Lock()
        # Один клиент на процесс: TCP+TLS-рукопожатие на каждый переход по
        # ссылке заметно дороже самого запроса.
        self._client = httpx.AsyncClient(timeout=settings.SHORTENER_AUTH_TIMEOUT)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def confirm_email(self, user_id: uuid.UUID) -> str:
        """Подтвердить адрес. Возвращает ``confirmed`` либо ``already_confirmed``."""
        data = await self._post(f'/api/v1/users/{user_id}/confirm-email')
        return str(data.get('status') or 'confirmed')

    async def _login(self) -> str:
        payload = {
            'login': settings.SHORTENER_AUTH_SERVICE_LOGIN,
            'password': settings.SHORTENER_AUTH_SERVICE_PASSWORD,
        }
        response = await self._client.post(f'{self._base_url}/api/v1/auth/login', json=payload, headers=self._headers())
        if response.status_code in (401, 403):
            raise ConfirmMisconfigured(f'служебная учётка не пускается в Auth: {response.status_code}')
        if response.status_code >= 400:
            raise ConfirmUnavailable(f'Auth ответил {response.status_code} на вход служебной учётки')
        token = _json(response).get('access_token')
        if not token:
            raise ConfirmMisconfigured('Auth не вернул access_token')
        self._token = token
        return token

    async def _token_for(self, stale: str | None = None) -> str:
        """Действующий токен. Логинится максимум одна корутина, прочие ждут её.

        ``stale`` — токен, на котором только что прилетела 401. Сравнение с ним
        вместо ``self._token = None`` не даёт опоздавшей корутине выбросить
        свежий токен, который сосед уже успел получить, и тем самым запустить
        второй круг входов: перелогинивается только тот, кто первым принёс
        негодное значение.

        Быстрая проверка ДО замка неслучайна: на горячем пути токен есть всегда,
        и брать замок на каждый переход по ссылке незачем.
        """
        token = self._token
        if token is not None and token != stale:
            return token

        async with self._token_lock:
            token = self._token
            if token is not None and token != stale:
                # Пока мы ждали замок, сосед уже вошёл — его токен и берём.
                return token
            return await self._login()

    async def _post(self, path: str) -> dict:
        url = f'{self._base_url}{path}'
        last_error: Exception | None = None
        stale: str | None = None

        for attempt in range(settings.SHORTENER_AUTH_MAX_ATTEMPTS):
            try:
                # Вход служебной учётки ВНУТРИ try: он тоже ходит по сети, и
                # httpx.ConnectError отсюда пролетал бы мимо всех except в
                # ручке редиректа — то есть человек с письмом получал бы голую
                # 500 вместо страницы 503 с Retry-After. Это ровно тот случай,
                # ради которого заведён ConfirmUnavailable.
                token = await self._token_for(stale)
                response = await self._client.post(url, headers=self._headers(token))
            except httpx.HTTPError as exc:
                last_error = exc
                await asyncio.sleep(compute_delay(attempt, start=0.1, factor=2, border=2))
                continue

            if response.status_code == 401:
                # Токен протух — переполучаем и повторяем. Именно 401: 403
                # означало бы «роль отобрали», и повтор бы не помог. Помечаем
                # негодным конкретное значение, а не обнуляем кэш: обнуление
                # затёрло бы токен, который параллельная корутина могла уже
                # обновить, и вернуло бы ровно ту пачку входов, от которой
                # заведён `_token_lock`.
                stale = token
                last_error = ConfirmMisconfigured('Auth ответил 401 на подтверждение')
                continue
            if response.status_code == 403:
                raise ConfirmMisconfigured('у служебной учётки нет роли email-confirmer')
            if response.status_code == 404:
                raise ConfirmRejected('Auth не знает такого пользователя')
            if response.status_code >= 500:
                last_error = ConfirmUnavailable(f'Auth ответил {response.status_code}')
                await asyncio.sleep(compute_delay(attempt, start=0.1, factor=2, border=2))
                continue
            if response.status_code >= 400:
                raise ConfirmMisconfigured(f'Auth ответил {response.status_code}: {response.text[:300]}')
            return _json(response)

        raise ConfirmUnavailable(f'Auth недоступен после {settings.SHORTENER_AUTH_MAX_ATTEMPTS} попыток: {last_error}')

    @staticmethod
    def _headers(token: str | None = None) -> dict[str, str]:
        # X-Request-Id ставится ВСЕГДА: у Auth RequestIdMiddleware работает в
        # режиме reject_400, и без заголовка вызов вернул бы 400 ещё до
        # обработчика. В нашем контексте идентификатор обычно есть — переход по
        # ссылке пришёл через nginx, — но подстраховка стоит одной строки.
        headers = {
            'Content-Type': 'application/json',
            'X-Request-Id': get_request_id() or str(uuid.uuid4()),
        }
        if token:
            headers['Authorization'] = f'Bearer {token}'
        return headers


def _json(response: httpx.Response) -> dict:
    """Тело ответа Auth словарём.

    Отдельная функция, потому что ``response.json()`` бросает ``ValueError`` на
    любом не-JSON теле — а такое приходит, когда между нами и Auth встал чей-то
    прокси с HTML-страницей ошибки. Без этого перехвата исключение уходило бы
    мимо всей таксономии ``Confirm*`` и превращалось в 500 у человека с письмом.
    """
    try:
        data = response.json()
    except ValueError as exc:
        raise ConfirmUnavailable(f'Auth ответил не-JSON телом: {response.text[:200]!r}') from exc
    if not isinstance(data, dict):
        raise ConfirmUnavailable(f'Auth ответил не объектом: {type(data).__name__}')
    return data
