import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    event,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from practix_auth.models.base import Base

user_roles = Table(
    'user_roles',
    Base.metadata,
    Column('user_id', UUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
    Column('role_id', UUID(as_uuid=True), ForeignKey('roles.id', ondelete='CASCADE'), primary_key=True),
)


class User(Base):
    """Модель пользователя."""

    __tablename__ = 'users'

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    login: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    # Пароль может отсутствовать у пользователей, вошедших только через соцсети (OAuth).
    password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    # Имя и фамилия — то, чем сервис нотификаций персонифицирует письмо. Nullable,
    # а не server_default='': пустая строка неотличима от «пользователь очистил
    # поле», а NULL честно говорит «мы никогда не спрашивали».
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Подтверждён ли адрес. Ставится не здесь: welcome-письмо несёт короткую
    # ссылку, шортенер её резолвит и зовёт POST /users/{id}/confirm-email.
    # NOT NULL с дефолтом, а не nullable: «неизвестно» — не состояние адреса.
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false(), default=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    roles: Mapped[list['Role']] = relationship(secondary=user_roles, back_populates='users', lazy='selectin')

    login_histories: Mapped[list['LoginHistory']] = relationship(back_populates='user', cascade='all, delete-orphan')

    social_accounts: Mapped[list['SocialAccount']] = relationship(back_populates='user', cascade='all, delete-orphan')


class Role(Base):
    """Модель роли."""

    __tablename__ = 'roles'

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)

    users: Mapped[list['User']] = relationship(secondary=user_roles, back_populates='roles')


class LoginHistory(Base):
    """Модель истории входов.

    Таблица секционирована в PostgreSQL по двум уровням (composite partitioning):
    верхний уровень — RANGE по ``auth_date`` (по годам), внутри каждого года —
    LIST по ``device_type`` (web / mobile / smart + DEFAULT-секция ``*_other``).

    Требование PostgreSQL: любой уникальный/первичный ключ секционированной
    таблицы обязан содержать все столбцы ключа секционирования каждого уровня,
    поэтому первичный ключ составной — ``(id, auth_date, device_type)``.
    """

    __tablename__ = 'login_history'
    __table_args__ = (
        PrimaryKeyConstraint('id', 'auth_date', 'device_type'),
        {
            'postgresql_partition_by': 'RANGE (auth_date)',
        },
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False
    )
    user_agent: Mapped[str] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str] = mapped_column(String(50), nullable=True)
    auth_date: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Тип устройства (web/mobile/smart) — ключ секционирования второго уровня.
    device_type: Mapped[str] = mapped_column(Text, nullable=False, server_default='web')

    user: Mapped['User'] = relationship(back_populates='login_histories')


# Устройства, под которые заводятся LIST-секции внутри каждого года.
_DEVICE_TYPES = ('web', 'mobile', 'smart')


def _partition_years() -> list[int]:
    """Годы, для которых заранее создаются секции.

    Всегда включает 2025 (историю из первой миграции), текущий год и следующий,
    чтобы вставки не падали на стыке года. Поддержку более далёкого будущего
    стоит автоматизировать через pg_partman.
    """
    current = datetime.utcnow().year
    return sorted({2025, current, current + 1})


def create_login_history_partitions(target, connection, **kw) -> None:
    """Создаёт двухуровневые секции для ``login_history``.

    Вешается на событие ``after_create`` таблицы, поэтому вызывается из
    ``Base.metadata.create_all`` (используется в тестах). SQLAlchemy умеет
    объявить только верхний уровень секционирования, поэтому годовые секции
    (RANGE) и вложенные секции по устройствам (LIST) создаём сырым DDL.
    """
    for year in _partition_years():
        year_table = f'login_history_{year}'
        connection.execute(
            text(
                f'CREATE TABLE IF NOT EXISTS "{year_table}" '
                f'PARTITION OF "login_history" '
                f"FOR VALUES FROM ('{year}-01-01') TO ('{year + 1}-01-01') "
                f'PARTITION BY LIST (device_type)'
            )
        )
        for device in _DEVICE_TYPES:
            connection.execute(
                text(
                    f'CREATE TABLE IF NOT EXISTS "{year_table}_{device}" '
                    f'PARTITION OF "{year_table}" FOR VALUES IN (\'{device}\')'
                )
            )
        connection.execute(text(f'CREATE TABLE IF NOT EXISTS "{year_table}_other" PARTITION OF "{year_table}" DEFAULT'))


event.listen(LoginHistory.__table__, 'after_create', create_login_history_partitions)


class SocialAccount(Base):
    """Связь пользователя с внешним провайдером OAuth (например, Яндекс)."""

    __tablename__ = 'social_accounts'
    __table_args__ = (UniqueConstraint('provider', 'provider_user_id', name='uq_social_provider_user'),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey('users.id', ondelete='CASCADE'), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped['User'] = relationship(back_populates='social_accounts')
