from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select

from api.v1.dependencies import PaginationParams, get_auth_service, get_current_user
from db.postgres import get_session
from models.entity import LoginHistory, User
from models.schemas import LoginHistoryResponse, TokenResponse, UserCreate, UserLogin, UserResponse
from services.auth_service import AuthService

router = APIRouter()


@router.post(
    '/register',
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary='Регистрация пользователя',
    description='Регистрация нового пользователя с проверкой сложности пароля и уникальности логина/email.',
    responses={
        status.HTTP_400_BAD_REQUEST: {'description': 'Слабый пароль или пользователь уже существует'},
    },
)
async def register(user_data: UserCreate, auth_service: AuthService = Depends(get_auth_service)):
    try:
        user = await auth_service.register(user_data)
        return user
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e


@router.post(
    '/login',
    response_model=TokenResponse,
    summary='Вход в систему',
    description='Аутентификация пользователя и выдача пары токенов (access и refresh).',
    responses={
        status.HTTP_401_UNAUTHORIZED: {'description': 'Неверный логин или пароль'},
    },
)
async def login(request: Request, user_data: UserLogin, auth_service: AuthService = Depends(get_auth_service)):
    user_agent = request.headers.get('user-agent', 'unknown')
    ip_address = request.client.host if request.client else 'unknown'
    try:
        tokens = await auth_service.login(user_data.login, user_data.password, user_agent, ip_address)
        return tokens
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e)) from e


@router.post(
    '/change-password',
    summary='Смена пароля',
    description='Изменение пароля текущего пользователя с инвалидацией всех активных сессий.',
    responses={
        status.HTTP_400_BAD_REQUEST: {'description': 'Неверный старый пароль или новый пароль слишком слабый'},
        status.HTTP_401_UNAUTHORIZED: {'description': 'Не авторизован'},
    },
)
async def change_password(
    old_password: str,
    new_password: str,
    user: User = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service),
):
    try:
        await auth_service.change_password(user, old_password, new_password)
        return {'msg': 'Пароль успешно изменен'}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e


@router.post(
    '/refresh',
    response_model=TokenResponse,
    summary='Обновление токенов',
    description='Получение новой пары токенов по валидному refresh-токену.',
    responses={
        status.HTTP_401_UNAUTHORIZED: {'description': 'Неверный или отозванный refresh-токен'},
    },
)
async def refresh(auth_service: AuthService = Depends(get_auth_service)):
    try:
        tokens = await auth_service.refresh_tokens()
        return tokens
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e)) from e


@router.post(
    '/logout',
    summary='Выход из системы',
    description='Инвалидация текущего access-токена (добавление в denylist).',
    responses={
        status.HTTP_401_UNAUTHORIZED: {'description': 'Не авторизован'},
    },
)
async def logout(auth_service: AuthService = Depends(get_auth_service)):
    await auth_service.logout()
    return {'msg': 'Выход выполнен успешно'}


@router.post(
    '/logout-all',
    summary='Выход на всех устройствах',
    description='Инвалидация всех активных сессий пользователя.',
    responses={
        status.HTTP_401_UNAUTHORIZED: {'description': 'Не авторизован'},
    },
)
async def logout_all(auth_service: AuthService = Depends(get_auth_service)):
    await auth_service.logout_all()
    return {'msg': 'Выход на всех устройствах выполнен успешно'}


@router.get(
    '/login-history',
    response_model=list[LoginHistoryResponse],
    summary='История входов',
    description='Получение истории посещений текущего пользователя.',
    responses={
        status.HTTP_401_UNAUTHORIZED: {'description': 'Не авторизован'},
    },
)
async def login_history(
    user: User = Depends(get_current_user),
    db=Depends(get_session),
    pagination: PaginationParams = Depends(PaginationParams),
):
    result = await db.execute(
        select(LoginHistory)
        .where(LoginHistory.user_id == user.id)
        .order_by(LoginHistory.auth_date.desc())
        .limit(pagination.page_size)
        .offset((pagination.page_number - 1) * pagination.page_size)
    )
    return result.scalars().all()
