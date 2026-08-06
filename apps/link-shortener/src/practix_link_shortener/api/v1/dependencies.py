"""Зависимости API."""

import secrets

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from practix_link_shortener.core.config import settings
from practix_link_shortener.db.postgres import async_session, get_session
from practix_link_shortener.services.auth_client import AuthClient
from practix_link_shortener.services.link_service import LinkService

#: Один клиент Auth на процесс — держит соединение и токен служебной учётки.
#: Пересоздавать его на запрос значило бы логиниться заново на каждый переход.
auth_client: AuthClient | None = None


async def get_link_service(session: AsyncSession = Depends(get_session)) -> LinkService:
    return LinkService(session)


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Фабрика сессий для работы ВНЕ запроса.

    Нужна фоновому учёту визитов: к моменту его выполнения FastAPI уже закрыл
    сессию запроса, и брать `async_session` напрямую из ``db.postgres`` нельзя —
    тогда его нечем подменить в тестах, а пул модульного движка привязан к тому
    event loop, в котором создавался.
    """
    return async_session


def get_auth_client() -> AuthClient:
    """Клиент Auth. Подменяется в тестах через ``dependency_overrides``."""
    if auth_client is None:  # pragma: no cover — собирается в lifespan
        raise RuntimeError('AuthClient не инициализирован')
    return auth_client


async def require_internal_token(authorization: str = Header(default='')) -> None:
    """Пропуск к ручке создания ссылок.

    Общий секрет, а не JWT: вызывающий — сервис, и заводить ради него
    пользователя с ролями значило бы держать второй механизм авторизации в
    сервисе, который пользовательских токенов вообще не проверяет.

    ``compare_digest``, а не ``==``: сравнение строк в Python обрывается на
    первом несовпавшем байте, и время ответа подсказывает, сколько символов
    угадано.

    Сравниваются БАЙТЫ: ``compare_digest`` на строках требует, чтобы обе были из
    ASCII, а Starlette декодирует заголовки как latin-1 — присланная кириллица
    давала бы ``TypeError``, то есть 500 вместо 401 на неверном токене.
    """
    prefix = 'Bearer '
    provided = authorization[len(prefix) :] if authorization.startswith(prefix) else ''
    if not secrets.compare_digest(provided.encode('utf-8'), settings.SHORTENER_INTERNAL_TOKEN.encode('utf-8')):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Неверный служебный токен')
