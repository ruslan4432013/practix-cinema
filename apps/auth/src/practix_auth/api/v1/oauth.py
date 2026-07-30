import secrets
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse

from practix_auth.api.v1.dependencies import get_current_user, get_oauth_service
from practix_auth.core.config import settings
from practix_auth.db.redis import get_redis
from practix_auth.models.entity import User
from practix_auth.models.schemas import SocialAccountResponse, TokenResponse
from practix_auth.services.oauth_providers import OAuthError, get_provider
from practix_auth.services.oauth_service import OAuthService

router = APIRouter()

_STATE_KEY = 'oauth:state:{state}'


def _require_provider(provider: str):
    """Провайдер по имени из URL; 404, если такой не зарегистрирован."""
    resolved = get_provider(provider)
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'Неизвестный OAuth-провайдер: {provider}',
        )
    return resolved


@router.get('/{provider}/login', summary='Начать вход через соцсеть')
async def oauth_login(provider: str):
    """Редирект на страницу авторизации провайдера (authorization code flow)."""
    resolved = _require_provider(provider)
    state = secrets.token_urlsafe(32)
    redis = await get_redis()
    await redis.set(_STATE_KEY.format(state=state), '1', ex=settings.OAUTH_STATE_TTL)

    url = resolved.build_authorize_url(state)
    return RedirectResponse(url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@router.get('/{provider}/callback', response_model=TokenResponse, summary='Callback OAuth-провайдера')
async def oauth_callback(
    provider: str,
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
    error_description: str | None = Query(None),
    oauth_service: OAuthService = Depends(get_oauth_service),
):
    """Обработка ответа провайдера: валидация state, обмен кода, выдача наших токенов."""
    resolved = _require_provider(provider)
    if error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_description or error,
        )
    if not code or not state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Отсутствует code или state',
        )

    redis = await get_redis()
    if not await redis.getdel(_STATE_KEY.format(state=state)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Неверный или истёкший state',
        )

    try:
        return await oauth_service.authenticate(resolved, code)
    except OAuthError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.get(
    '/social/accounts',
    response_model=list[SocialAccountResponse],
    summary='Список привязанных соцсетей',
)
async def list_social_accounts(
    user: User = Depends(get_current_user),
    oauth_service: OAuthService = Depends(get_oauth_service),
):
    """Список соцсетей, привязанных к аккаунту текущего пользователя."""
    return await oauth_service.list_social_accounts(user)


@router.delete(
    '/social/accounts/{account_id}',
    status_code=status.HTTP_204_NO_CONTENT,
    summary='Открепить аккаунт соцсети',
)
async def unlink_social_account(
    account_id: UUID,
    user: User = Depends(get_current_user),
    oauth_service: OAuthService = Depends(get_oauth_service),
):
    """Открепление соцсети от аккаунта текущего пользователя."""
    try:
        await oauth_service.unlink_social_account(user, account_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
