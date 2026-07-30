import uuid
from datetime import datetime

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


class UserCreate(UserBase):
    password: str


class UserLogin(BaseModel):
    login: str
    password: str


class UserResponse(UserBase):
    id: uuid.UUID
    created_at: datetime
    roles: list[RoleResponse] = []

    class Config:
        from_attributes = True


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
