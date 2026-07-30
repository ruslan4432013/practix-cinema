"""Провайдеры OAuth (сторона потребителя) как стратегии + реестр.

Каждый провайдер инкапсулирует свои URL/креды, схему авторизации и маппинг
профиля в нормализованный вид. Имя провайдера приходит из URL и по нему в
реестре выбирается нужная реализация, поэтому одна пара ручек обслуживает всех,
а добавление нового провайдера = подкласс + блок настроек + регистрация.
"""

from dataclasses import dataclass
from typing import ClassVar
from urllib.parse import urlencode

import httpx

from practix_auth.core.config import settings


class OAuthError(Exception):
    """Ошибка взаимодействия с OAuth-провайдером (сетевой сбой или ответ не 200)."""


@dataclass
class OAuthProfile:
    """Нормализованный профиль пользователя, независимый от провайдера."""

    provider_user_id: str
    email: str | None = None
    login: str | None = None


class BaseOAuthProvider:
    """Базовая стратегия провайдера: провайдер-нейтральная HTTP-логика.

    Конкретные провайдеры задают URL/креды/схему авторизации через атрибуты и
    реализуют :meth:`extract_profile` (маппинг ответа userinfo в ``OAuthProfile``).
    """

    name: str
    client_id: str
    client_secret: str
    redirect_uri: str
    authorize_url: str
    token_url: str
    userinfo_url: str
    auth_scheme: str = 'Bearer'
    # ClassVar, а не поле экземпляра: значение общее для всех вызовов провайдера
    # и никогда не мутируется — иначе один общий словарь протёк бы между ними.
    userinfo_params: ClassVar[dict[str, str]] = {}

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout

    def build_authorize_url(self, state: str) -> str:
        """URL страницы авторизации провайдера (authorization code flow)."""
        params = {
            'response_type': 'code',
            'client_id': self.client_id,
            'redirect_uri': self.redirect_uri,
            'state': state,
        }
        return f'{self.authorize_url}?{urlencode(params)}'

    async def exchange_code(self, code: str) -> dict:
        """Обмен authorization code на OAuth-токен."""
        data = {
            'grant_type': 'authorization_code',
            'code': code,
            'client_id': self.client_id,
            'client_secret': self.client_secret,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(self.token_url, data=data)
        except httpx.HTTPError as exc:
            raise OAuthError(f'Не удалось обратиться к {self.name} OAuth: {exc}') from exc
        if response.status_code != 200:
            raise OAuthError(f'Обмен кода не удался: {response.text}')
        return response.json()  # {access_token, refresh_token, token_type, expires_in}

    async def get_user_info(self, access_token: str) -> dict:
        """Получение профиля пользователя по OAuth-токену."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    self.userinfo_url,
                    params=self.userinfo_params,
                    headers={'Authorization': f'{self.auth_scheme} {access_token}'},
                )
        except httpx.HTTPError as exc:
            raise OAuthError(f'Не удалось получить профиль {self.name}: {exc}') from exc
        if response.status_code != 200:
            raise OAuthError(f'Запрос профиля не удался: {response.text}')
        return response.json()

    def extract_profile(self, info: dict) -> OAuthProfile:
        """Маппинг ответа userinfo в нормализованный профиль (переопределяется)."""
        raise NotImplementedError


class YandexProvider(BaseOAuthProvider):
    """Провайдер Яндекс OAuth."""

    name = 'yandex'
    # Яндекс использует схему `OAuth`, а не `Bearer`, и требует format=json.
    auth_scheme = 'OAuth'
    userinfo_params: ClassVar[dict[str, str]] = {'format': 'json'}

    def __init__(self, timeout: float = 10.0):
        super().__init__(timeout)
        self.client_id = settings.YANDEX_CLIENT_ID
        self.client_secret = settings.YANDEX_CLIENT_SECRET
        self.redirect_uri = settings.YANDEX_REDIRECT_URI
        self.authorize_url = settings.YANDEX_AUTHORIZE_URL
        self.token_url = settings.YANDEX_TOKEN_URL
        self.userinfo_url = settings.YANDEX_USERINFO_URL

    def extract_profile(self, info: dict) -> OAuthProfile:
        return OAuthProfile(
            provider_user_id=str(info.get('id') or ''),
            email=info.get('default_email'),
            login=info.get('login'),
        )


# Реестр доступных провайдеров. Чтобы добавить нового — заведите подкласс
# BaseOAuthProvider и зарегистрируйте его здесь по имени.
_REGISTRY: dict[str, BaseOAuthProvider] = {provider.name: provider for provider in (YandexProvider(),)}


def get_provider(name: str) -> BaseOAuthProvider | None:
    """Провайдер по имени из URL или None, если такой не зарегистрирован."""
    return _REGISTRY.get(name)
