from async_fastapi_jwt_auth import AuthJWT
from async_fastapi_jwt_auth.exceptions import AuthJWTException
from fastapi import APIRouter, Depends, HTTPException, status

from api.v1.dependencies import (
    SUPERUSER_ROLES,
    get_auth_service,
    get_current_user,
)
from models.entity import User
from models.schemas import (
    PermissionCheckRequest,
    PermissionCheckResponse,
    UserCredentialsUpdate,
    UserResponse,
)
from services.auth_service import AuthService

router = APIRouter()


@router.get(
    '/me',
    response_model=UserResponse,
    summary='Текущий пользователь',
    description='Получение информации о текущем авторизованном пользователе.',
    tags=['Пользователи'],
    responses={
        status.HTTP_401_UNAUTHORIZED: {'description': 'Не авторизован'},
    },
)
async def get_me(user: User = Depends(get_current_user)):
    return user


@router.patch(
    '/me/credentials',
    response_model=UserResponse,
    summary='Смена логина/email',
    description=('Изменение логина и/или email текущего пользователя. Требуется подтверждение текущим паролем.'),
    tags=['Пользователи'],
    responses={
        status.HTTP_400_BAD_REQUEST: {'description': 'Неверные данные'},
        status.HTTP_401_UNAUTHORIZED: {'description': 'Не авторизован'},
    },
)
async def change_credentials(
    payload: UserCredentialsUpdate,
    user: User = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
):
    try:
        updated = await auth_service.change_credentials(
            user=user,
            current_password=payload.current_password,
            new_login=payload.login,
            new_email=payload.email,
        )
        return updated
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e


@router.post(
    '/check-permissions',
    response_model=PermissionCheckResponse,
    summary='Проверка прав (межсервисный эндпоинт)',
    description=(
        'Проверяет валидность access-токена и наличие у пользователя требуемых ролей. '
        'Предназначен для межсервисного использования другими сервисами платформы. '
        'Суперпользователь (роли admin/superuser) автоматически проходит любую проверку.'
    ),
    tags=['Пользователи'],
)
async def check_permissions(
    payload: PermissionCheckRequest,
    authorize: AuthJWT = Depends(),
):
    try:
        decoded = await authorize.get_raw_jwt(payload.access_token)
    except AuthJWTException:
        return PermissionCheckResponse(allowed=False)

    if not decoded:
        return PermissionCheckResponse(allowed=False)

    user_id = decoded.get('sub')
    roles = decoded.get('roles', []) or []
    roles_set = set(roles)
    is_superuser = bool(roles_set & SUPERUSER_ROLES)

    if is_superuser or not payload.required_roles:
        allowed = True
    else:
        allowed = bool(roles_set.intersection(payload.required_roles))

    return PermissionCheckResponse(
        allowed=allowed,
        user_id=user_id,
        roles=list(roles),
        is_superuser=is_superuser,
    )
