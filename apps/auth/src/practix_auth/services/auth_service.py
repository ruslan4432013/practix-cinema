import uuid
from datetime import datetime

from async_fastapi_jwt_auth import AuthJWT
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from practix_auth.core.config import settings
from practix_auth.db.redis import get_redis
from practix_auth.models.entity import LoginHistory, Role, User
from practix_auth.models.schemas import UserCreate
from practix_auth.services.security import check_password_strength, hash_password, verify_password
from practix_auth.services.user_agent import detect_device_type

#: Подтверждение адреса одним оператором. ``prev`` берёт строку под
#: ``FOR UPDATE``, поэтому два одновременных перехода по ссылке (человек и
#: сканер почтового клиента) выстраиваются в очередь, а не подтверждают дважды.
#: Пустой результат означает «пользователя нет» — этим сырым SQL и отличается
#: от ORM-версии, где пришлось бы отдельно проверять существование.
_CONFIRM_EMAIL = text("""
    WITH prev AS (
        SELECT id, email_verified FROM users WHERE id = :user_id FOR UPDATE
    )
    UPDATE users u
       SET email_verified = true,
           email_verified_at = coalesce(u.email_verified_at, now())
      FROM prev
     WHERE u.id = prev.id
    RETURNING prev.email_verified AS was_verified
""")


class AuthService:
    def __init__(self, db: AsyncSession, authorize: AuthJWT):
        self.db = db
        self.authorize = authorize

    async def register(self, user_create: UserCreate) -> User:
        """Регистрация нового пользователя."""
        if not check_password_strength(user_create.password):
            raise ValueError('Пароль слишком слабый')

        # Проверка существования пользователя
        stmt = select(User).where((User.login == user_create.login) | (User.email == user_create.email))
        result = await self.db.execute(stmt)
        existing_user = result.scalar_one_or_none()
        if existing_user:
            raise ValueError('Пользователь с таким логином или email уже существует')

        hashed_pwd = hash_password(user_create.password)
        new_user = User(
            login=user_create.login,
            email=user_create.email,
            password=hashed_pwd,
            first_name=user_create.first_name,
            last_name=user_create.last_name,
        )

        # Назначение роли по умолчанию
        role_result = await self.db.execute(select(Role).where(Role.name == settings.DEFAULT_ROLE_NAME))
        default_role = role_result.scalar_one_or_none()
        if not default_role:
            default_role = Role(name=settings.DEFAULT_ROLE_NAME, description='Роль по умолчанию')
            self.db.add(default_role)
            await self.db.flush()
        new_user.roles.append(default_role)

        self.db.add(new_user)
        await self.db.commit()
        await self.db.refresh(new_user)
        return new_user

    async def _add_to_user_sessions(self, user_id: str, jti: str):
        """Добавление JTI токена в список сессий пользователя в Redis."""
        redis = await get_redis()
        await redis.sadd(f'user:{user_id}:sessions', jti)
        await redis.expire(f'user:{user_id}:sessions', settings.REFRESH_TOKEN_EXPIRES)

    async def _issue_tokens(self, user: User) -> dict:
        """Генерация пары access/refresh токенов и регистрация их JTI в Redis.

        Единая точка выдачи токенов — используется обычным логином и входом
        через соцсети (OAuth), чтобы гарантировать одинаковую семантику
        claims, denylist и списка сессий.
        """
        user_claims = {'roles': [role.name for role in user.roles]}
        access_token = await self.authorize.create_access_token(subject=str(user.id), user_claims=user_claims)
        refresh_token = await self.authorize.create_refresh_token(subject=str(user.id))

        access_jti = await self.authorize.get_jti(access_token)
        refresh_jti = await self.authorize.get_jti(refresh_token)
        await self._add_to_user_sessions(str(user.id), access_jti)
        await self._add_to_user_sessions(str(user.id), refresh_jti)

        return {'access_token': access_token, 'refresh_token': refresh_token}

    async def login(self, login: str, password: str, user_agent: str, ip_address: str) -> dict:
        """Аутентификация пользователя и генерация токенов."""
        result = await self.db.execute(select(User).where(User.login == login))
        user = result.scalar_one_or_none()

        if not user or user.password is None or not verify_password(password, user.password):
            raise ValueError('Неверный логин или пароль')

        # Сохранение истории входа. device_type — ключ секционирования по
        # устройству, определяется из User-Agent.
        history = LoginHistory(
            user_id=user.id,
            user_agent=user_agent,
            ip_address=ip_address,
            auth_date=datetime.utcnow(),
            device_type=detect_device_type(user_agent),
        )
        self.db.add(history)
        await self.db.commit()

        # Генерация токенов
        return await self._issue_tokens(user)

    async def refresh_tokens(self) -> dict:
        """Обновление access и refresh токенов."""
        await self.authorize.jwt_refresh_token_required()

        user_id = await self.authorize.get_jwt_subject()

        # Получаем пользователя для актуального списка ролей
        result = await self.db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            raise ValueError('Пользователь не найден')

        # Ротация: отзыв старого refresh токена
        jti = (await self.authorize.get_raw_jwt())['jti']
        redis = await get_redis()
        await redis.set(jti, 'revoked', ex=settings.REFRESH_TOKEN_EXPIRES)
        await redis.srem(f'user:{user_id}:sessions', jti)

        # Генерация новых токенов
        user_claims = {'roles': [role.name for role in user.roles]}
        new_access_token = await self.authorize.create_access_token(subject=user_id, user_claims=user_claims)
        new_refresh_token = await self.authorize.create_refresh_token(subject=user_id)

        # Сохранение новых JTI
        access_jti = await self.authorize.get_jti(new_access_token)
        refresh_jti = await self.authorize.get_jti(new_refresh_token)
        await self._add_to_user_sessions(user_id, access_jti)
        await self._add_to_user_sessions(user_id, refresh_jti)

        return {'access_token': new_access_token, 'refresh_token': new_refresh_token}

    async def change_credentials(
        self,
        user: User,
        current_password: str,
        new_login: str | None = None,
        new_email: str | None = None,
    ) -> User:
        """Смена логина и/или email с проверкой пароля и уникальности значений."""
        if user.password is None:
            raise ValueError('Для аккаунта не установлен пароль')
        if not verify_password(current_password, user.password):
            raise ValueError('Неверный пароль')

        if not new_login and not new_email:
            raise ValueError('Не указаны новые значения логина или email')

        # Проверка уникальности
        conditions = []
        if new_login and new_login != user.login:
            conditions.append(User.login == new_login)
        if new_email and new_email != user.email:
            conditions.append(User.email == new_email)

        if conditions:
            from sqlalchemy import or_

            stmt = select(User).where(or_(*conditions), User.id != user.id)
            result = await self.db.execute(stmt)
            if result.scalar_one_or_none():
                raise ValueError('Логин или email уже заняты')

        if new_login:
            user.login = new_login
        if new_email and new_email != user.email:
            user.email = new_email
            # Подтверждён был СТАРЫЙ адрес. Оставить флаг — значит объявить
            # подтверждённым ящик, который человек, возможно, не открывал ни
            # разу; на этом строятся восстановление пароля и рассылка.
            user.email_verified = False
            user.email_verified_at = None

        await self.db.commit()
        await self.db.refresh(user)
        return user

    async def change_password(self, user: User, old_password: str, new_password: str):
        """Смена пароля с отзывом всех активных сессий."""
        if user.password is None:
            raise ValueError('Для аккаунта не установлен пароль')
        if not verify_password(old_password, user.password):
            raise ValueError('Неверный старый пароль')

        if not check_password_strength(new_password):
            raise ValueError('Новый пароль слишком слабый')

        user.password = hash_password(new_password)
        await self.db.commit()

        # Для безопасности инвалидируем все сессии
        await self.authorize.set_jwt_subject(str(user.id))
        await self.logout_all()

    async def logout(self):
        """Выход пользователя (отзыв текущего токена)."""
        await self.authorize.jwt_required()
        jti = (await self.authorize.get_raw_jwt())['jti']
        user_id = await self.authorize.get_jwt_subject()
        redis = await get_redis()
        await redis.set(jti, 'revoked', ex=settings.ACCESS_TOKEN_EXPIRES)
        await redis.srem(f'user:{user_id}:sessions', jti)

    async def logout_all(self):
        """Выход со всех устройств (отзыв всех токенов пользователя)."""
        await self.authorize.jwt_required()
        user_id = await self.authorize.get_jwt_subject()

        redis = await get_redis()
        sessions_key = f'user:{user_id}:sessions'
        jtis = await redis.smembers(sessions_key)
        for jti in jtis:
            await redis.set(jti, 'revoked', ex=settings.REFRESH_TOKEN_EXPIRES)
        await redis.delete(sessions_key)

    async def list_users(self, *, limit: int, offset: int) -> tuple[list[User], int]:
        """Страница пользователей для межсервисной выгрузки.

        Нужна сервису нотификаций: он держит СВОЮ витрину контактов и обновляет
        её периодической синхронизацией, чтобы рассылка на десятки тысяч адресов
        не ходила в Auth за каждым получателем и не роняла вместе с ним логин на
        всём сайте.

        Сортировка по ``created_at, id`` — стабильная: без неё две соседние
        страницы могут вернуть одну и ту же запись и пропустить другую.
        """
        total = await self.db.scalar(select(func.count()).select_from(User))
        stmt = select(User).order_by(User.created_at, User.id).limit(limit).offset(offset)
        result = await self.db.execute(stmt)
        return list(result.scalars().unique().all()), int(total or 0)

    async def lookup_users(self, ids: list[uuid.UUID]) -> list[User]:
        """Личные данные пачкой по списку id.

        Это то, чем воркер рассылки персонифицирует письмо: у него на руках
        только ``user_id``, а имя, фамилия и адрес живут здесь. Один запрос на
        пачку, а не по запросу на получателя, — иначе рассылка превращается в
        поток из тысяч обращений и кладёт вход на сайт вместе с собой.

        ``roles`` подгружается ``lazy='selectin'``, то есть добавляет ровно один
        запрос на всю пачку, а не по одному на пользователя.
        """
        stmt = select(User).where(User.id.in_(ids)).order_by(User.id)
        result = await self.db.execute(stmt)
        return list(result.scalars().unique().all())

    async def confirm_email(self, user_id: uuid.UUID) -> bool | None:
        """Пометить адрес подтверждённым.

        Возвращает ``True``, если подтверждение произошло сейчас, ``False`` —
        если адрес уже был подтверждён, и ``None``, если пользователя нет.

        Один оператор вместо «прочитать — проверить — записать»: между чтением и
        записью успевает пройти второй клик (или префетч почтового клиента).
        CTE нужна ровно затем, чтобы вернуть ПРЕЖНЕЕ значение — RETURNING отдаёт
        новое, а вызывающему нужно отличить первое подтверждение от повторного.
        ``coalesce`` сохраняет момент первого: повторный переход не переписывает
        историю.
        """
        result = await self.db.execute(_CONFIRM_EMAIL, {'user_id': user_id})
        row = result.first()
        await self.db.commit()
        if row is None:
            return None
        return not row.was_verified

    async def update_profile(self, *, user: User, first_name: str | None, last_name: str | None) -> User:
        """Смена имени и фамилии.

        ``None`` означает «не трогать», а не «очистить»: PATCH обязан уметь
        менять одно поле, не зная про второе.
        """
        if first_name is not None:
            user.first_name = first_name
        if last_name is not None:
            user.last_name = last_name

        await self.db.commit()
        await self.db.refresh(user)
        return user
