import uuid

from async_fastapi_jwt_auth import AuthJWT
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from models.entity import Role, SocialAccount, User
from services.auth_service import AuthService
from services.oauth_providers import BaseOAuthProvider, OAuthError, OAuthProfile


class OAuthService:
    """Оркестрация входа через соцсети: провайдер → пользователь → наши токены."""

    def __init__(self, db: AsyncSession, authorize: AuthJWT):
        self.db = db
        self.authorize = authorize
        self.auth_service = AuthService(db, authorize)

    async def authenticate(self, provider: BaseOAuthProvider, code: str) -> dict:
        """Полный цикл: код → токен провайдера → профиль → пользователь → наши JWT."""
        token_data = await provider.exchange_code(code)
        access_token = token_data.get('access_token')
        if not access_token:
            raise OAuthError(f'{provider.name} не вернул access_token')

        info = await provider.get_user_info(access_token)
        profile = provider.extract_profile(info)
        if not profile.provider_user_id:
            raise OAuthError(f'{provider.name} не вернул идентификатор пользователя')

        user = await self._find_or_create_user(provider, profile)
        return await self.auth_service._issue_tokens(user)

    async def list_social_accounts(self, user: User) -> list[SocialAccount]:
        """Список привязанных соцсетей пользователя (по времени привязки)."""
        result = await self.db.execute(
            select(SocialAccount).where(SocialAccount.user_id == user.id).order_by(SocialAccount.created_at)
        )
        return list(result.scalars().all())

    async def unlink_social_account(self, user: User, account_id: uuid.UUID) -> None:
        """
        Открепление соцсети от аккаунта.

        Проверяет владение записью и защищает от блокировки входа: если у
        пользователя нет пароля и это последний способ входа — открепление
        запрещено.
        """
        result = await self.db.execute(
            select(SocialAccount).where(
                SocialAccount.id == account_id,
                SocialAccount.user_id == user.id,
            )
        )
        account = result.scalar_one_or_none()
        if account is None:
            raise ValueError('Привязанный аккаунт не найден')

        # Защита от блокировки: у пользователя без пароля должен остаться
        # хотя бы один способ входа.
        if user.password is None:
            count_result = await self.db.execute(
                select(func.count()).select_from(SocialAccount).where(SocialAccount.user_id == user.id)
            )
            if count_result.scalar_one() <= 1:
                raise ValueError('Нельзя открепить последний способ входа. Сначала установите пароль.')

        await self.db.delete(account)
        await self.db.commit()

    async def _find_or_create_user(self, provider: BaseOAuthProvider, profile: OAuthProfile) -> User:
        provider_user_id = profile.provider_user_id

        # 1. Уже связанный аккаунт?
        result = await self.db.execute(
            select(SocialAccount).where(
                SocialAccount.provider == provider.name,
                SocialAccount.provider_user_id == provider_user_id,
            )
        )
        social = result.scalar_one_or_none()
        if social:
            user_result = await self.db.execute(select(User).where(User.id == social.user_id))
            return user_result.scalar_one()

        # 2. Привязка по email к существующему пользователю (по выбору пользователя).
        #    Допущение: провайдер верифицирует email аккаунта. Это создаёт минимальный
        #    риск захвата аккаунта, если бы провайдер отдавал неподтверждённые email.
        email = profile.email
        if email:
            existing_result = await self.db.execute(select(User).where(User.email == email))
            existing_user = existing_result.scalar_one_or_none()
            if existing_user:
                self.db.add(
                    SocialAccount(
                        user_id=existing_user.id,
                        provider=provider.name,
                        provider_user_id=provider_user_id,
                    )
                )
                await self.db.commit()
                return existing_user

        # 3. Создание нового пользователя без пароля.
        login = await self._unique_login(profile.login or f'{provider.name}_{provider_user_id}')
        user_email = await self._unique_email(email, provider.name, provider_user_id)
        user = User(login=login, email=user_email, password=None)

        # Роль по умолчанию (как в AuthService.register).
        role_result = await self.db.execute(select(Role).where(Role.name == settings.DEFAULT_ROLE_NAME))
        default_role = role_result.scalar_one_or_none()
        if not default_role:
            default_role = Role(name=settings.DEFAULT_ROLE_NAME, description='Роль по умолчанию')
            self.db.add(default_role)
            await self.db.flush()
        user.roles.append(default_role)

        self.db.add(user)
        await self.db.flush()  # получаем user.id
        self.db.add(
            SocialAccount(
                user_id=user.id,
                provider=provider.name,
                provider_user_id=provider_user_id,
            )
        )
        await self.db.commit()
        await self.db.refresh(user)
        return user

    async def _unique_login(self, base: str) -> str:
        """Подбор свободного логина (login уникален)."""
        candidate = base
        while True:
            result = await self.db.execute(select(User).where(User.login == candidate))
            if not result.scalar_one_or_none():
                return candidate
            candidate = f'{base}_{uuid.uuid4().hex[:6]}'

    async def _unique_email(self, email: str | None, provider_name: str, provider_user_id: str) -> str:
        """Подбор свободного email; при отсутствии — синтетический (email NOT NULL unique)."""
        base = email or f'{provider_name}_{provider_user_id}@{provider_name}.local'
        candidate = base
        while True:
            result = await self.db.execute(select(User).where(User.email == candidate))
            if not result.scalar_one_or_none():
                return candidate
            local, _, domain = base.partition('@')
            candidate = f'{local}+{uuid.uuid4().hex[:6]}@{domain}'
