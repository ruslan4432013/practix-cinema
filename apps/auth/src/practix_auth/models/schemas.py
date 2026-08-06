import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field


class RoleBase(BaseModel):
    name: str
    description: str | None = None


class RoleCreate(RoleBase):
    pass


class RoleUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class RoleResponse(RoleBase):
    id: uuid.UUID

    class Config:
        from_attributes = True


class UserBase(BaseModel):
    login: str
    email: EmailStr
    # Необязательные: пользователь мог не называть имени, а у заведённых до этой
    # версии его нет физически. Наследуются и в UserCreate (регистрация их
    # принимает), и в UserResponse (выдача их отдаёт) — одним объявлением.
    first_name: str | None = Field(None, max_length=128)
    last_name: str | None = Field(None, max_length=128)


class UserCreate(UserBase):
    password: str


class UserLogin(BaseModel):
    login: str
    password: str


class UserResponse(UserBase):
    id: uuid.UUID
    created_at: datetime
    roles: list[RoleResponse] = []
    # Объявлено здесь, а не в UserBase: UserBase наследует и UserCreate, а
    # регистрация, принимающая «мой адрес уже подтверждён», — это и есть
    # отсутствие подтверждения. Поле только на выдаче.
    email_verified: bool = False

    class Config:
        from_attributes = True


class UserListResponse(BaseModel):
    """Страница списка пользователей для межсервисной выгрузки.

    ``total`` отдаётся вместе со страницей: потребителю (сервису нотификаций)
    нужно знать, сколько ещё идти, не запрашивая последнюю страницу вслепую.
    """

    items: list[UserResponse]
    total: int
    limit: int
    offset: int


class UserLookupRequest(BaseModel):
    """Запрос личных данных пачкой (для межсервисного использования).

    POST, а не ``GET ?ids=...``: 500 UUID — это около 20 КБ строки запроса, а
    h11 и nginx режут стартовую строку с заголовками на 8-16 КБ. Отдавать
    выборку по списку через тело — единственный способ не упереться в лимит,
    который проявится только на большой рассылке.
    """

    ids: list[uuid.UUID] = Field(min_length=1, max_length=500)


class UserLookupResponse(BaseModel):
    """Найденные пользователи и явный список ненайденных.

    ``missing`` перечисляется отдельно, а не вычисляется потребителем: сервису
    нотификаций нужно отличать «пользователь удалён» от «я передал не тот id» —
    на этом различии строится причина пропуска ``unknown_user`` в журнале
    доставки.
    """

    items: list[UserResponse]
    missing: list[uuid.UUID]


class UserProfileUpdate(BaseModel):
    """Смена имени и фамилии текущего пользователя.

    Без ``current_password``, в отличие от смены логина и email: отображаемое
    имя — не учётные данные, и подтверждать его паролем незачем.
    """

    first_name: str | None = Field(None, max_length=128)
    last_name: str | None = Field(None, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str


class LoginHistoryResponse(BaseModel):
    user_id: uuid.UUID
    user_agent: str | None
    ip_address: str | None
    auth_date: datetime
    device_type: str | None = None

    class Config:
        from_attributes = True


class UserCredentialsUpdate(BaseModel):
    """Схема для смены логина и/или email текущего пользователя."""

    login: str | None = Field(None, min_length=3, max_length=64)
    email: EmailStr | None = None
    current_password: str


class SocialAccountResponse(BaseModel):
    """Привязанный аккаунт соцсети в личном кабинете."""

    id: uuid.UUID
    provider: str
    provider_user_id: str
    created_at: datetime

    class Config:
        from_attributes = True


class PermissionCheckRequest(BaseModel):
    """Запрос на проверку прав (для межсервисного использования)."""

    access_token: str
    required_roles: list[str] = Field(default_factory=list)


class PermissionCheckResponse(BaseModel):
    allowed: bool
    user_id: uuid.UUID | None = None
    roles: list[str] = Field(default_factory=list)
    is_superuser: bool = False


class EmailConfirmResponse(BaseModel):
    """Результат подтверждения адреса.

    ``already_confirmed`` — это успех, а не 409: подтверждение идемпотентно, и
    у вызывающего (сервиса сокращения ссылок) должен быть один путь успеха.
    Пользователь кликает по ссылке дважды, а почтовый клиент — ещё и до него.
    """

    status: Literal['confirmed', 'already_confirmed']
    user_id: uuid.UUID
