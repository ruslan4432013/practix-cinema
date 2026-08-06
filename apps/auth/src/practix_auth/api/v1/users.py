import uuid

from async_fastapi_jwt_auth import AuthJWT
from async_fastapi_jwt_auth.exceptions import AuthJWTException
from fastapi import APIRouter, Depends, HTTPException, Query, status

from practix_auth.api.v1.dependencies import (
    SUPERUSER_ROLES,
    get_auth_service,
    get_current_user,
    role_required,
)
from practix_auth.core.roles import EMAIL_CONFIRMER_ROLE
from practix_auth.models.entity import User
from practix_auth.models.schemas import (
    EmailConfirmResponse,
    PermissionCheckRequest,
    PermissionCheckResponse,
    UserCredentialsUpdate,
    UserListResponse,
    UserLookupRequest,
    UserLookupResponse,
    UserProfileUpdate,
    UserResponse,
)
from practix_auth.services.auth_service import AuthService

router = APIRouter()


@router.get(
    '',
    response_model=UserListResponse,
    summary='Список пользователей (межсервисный эндпоинт)',
    description=(
        'Постраничная выгрузка пользователей. Нужна сервису нотификаций: он держит собственную '
        'витрину контактов и обновляет её синхронизацией, чтобы рассылка не ходила в Auth за '
        'каждым получателем. Доступ только у администратора.'
    ),
    tags=['Пользователи'],
    responses={
        status.HTTP_401_UNAUTHORIZED: {'description': 'Не авторизован'},
        status.HTTP_403_FORBIDDEN: {'description': 'Недостаточно прав'},
    },
)
async def list_users(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    auth_service: AuthService = Depends(get_auth_service),
    _roles: list[str] = Depends(role_required(['admin'])),
):
    users, total = await auth_service.list_users(limit=limit, offset=offset)
    # Словарь, а не UserListResponse(...): преобразование ORM-объектов в схемы
    # делает response_model FastAPI, у которого для этого есть from_attributes.
    return {'items': users, 'total': total, 'limit': limit, 'offset': offset}


# ВНИМАНИЕ: все литеральные пути (`/lookup`, `/me`, `/check-permissions`)
# обязаны быть объявлены ВЫШЕ любого будущего `/{user_id}`. FastAPI выбирает
# первый подошедший маршрут, и `/{user_id}` перехватит их все, отвечая 422 на
# «lookup» как на невалидный UUID.
@router.post(
    '/lookup',
    response_model=UserLookupResponse,
    summary='Личные данные пачкой (межсервисный эндпоинт)',
    description=(
        'Возвращает пользователей по списку id одним запросом. Нужна воркеру рассылки: он получает '
        'из очереди только идентификатор получателя и сам приходит сюда за именем, фамилией и '
        'адресом, чтобы собрать персональное письмо. Доступ только у администратора.'
    ),
    tags=['Пользователи'],
    responses={
        status.HTTP_401_UNAUTHORIZED: {'description': 'Не авторизован'},
        status.HTTP_403_FORBIDDEN: {'description': 'Недостаточно прав'},
    },
)
async def lookup_users(
    payload: UserLookupRequest,
    auth_service: AuthService = Depends(get_auth_service),
    _roles: list[str] = Depends(role_required(['admin'])),
):
    found = await auth_service.lookup_users(payload.ids)
    known = {user.id for user in found}
    return {'items': found, 'missing': [user_id for user_id in payload.ids if user_id not in known]}


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


@router.patch(
    '/me/profile',
    response_model=UserResponse,
    summary='Смена имени и фамилии',
    description=(
        'Изменение имени и/или фамилии текущего пользователя. Подтверждение паролем не требуется: '
        'отображаемое имя — не учётные данные. Именно эти поля подставляет в письмо сервис '
        'нотификаций.'
    ),
    tags=['Пользователи'],
    responses={
        status.HTTP_401_UNAUTHORIZED: {'description': 'Не авторизован'},
    },
)
async def change_profile(
    payload: UserProfileUpdate,
    user: User = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
):
    return await auth_service.update_profile(
        user=user,
        first_name=payload.first_name,
        last_name=payload.last_name,
    )


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


# Единственный маршрут с параметром в первом сегменте — и он объявлен последним,
# ниже всех литеральных, как требует комментарий выше. Перехватить он их не
# может (у него два сегмента, а второй — литерал `confirm-email`), но порядок
# держим тот же: инвариант проще соблюдать, чем каждый раз перепроверять.
@router.post(
    '/{user_id}/confirm-email',
    response_model=EmailConfirmResponse,
    summary='Подтверждение адреса (межсервисный эндпоинт)',
    description=(
        'Помечает email пользователя подтверждённым. Зовётся сервисом сокращения ссылок, когда '
        'человек перешёл по короткой ссылке из welcome-письма. Идемпотентна: повторный переход '
        'возвращает 200 и `already_confirmed`, а не 409 — по ссылке кликают дважды, и до человека '
        'по ней ходит сканер почтового клиента. Доступ по узкой роли `email-confirmer`, а не по '
        '`admin`: сервису нужна ровно одна возможность, а не вся административная поверхность.'
    ),
    tags=['Пользователи'],
    responses={
        status.HTTP_401_UNAUTHORIZED: {'description': 'Не авторизован'},
        status.HTTP_403_FORBIDDEN: {'description': 'Недостаточно прав'},
        status.HTTP_404_NOT_FOUND: {'description': 'Пользователь не найден'},
    },
)
async def confirm_email(
    user_id: uuid.UUID,
    auth_service: AuthService = Depends(get_auth_service),
    _roles: list[str] = Depends(role_required([EMAIL_CONFIRMER_ROLE])),
):
    confirmed = await auth_service.confirm_email(user_id)
    if confirmed is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Пользователь не найден')
    return {'status': 'confirmed' if confirmed else 'already_confirmed', 'user_id': user_id}
