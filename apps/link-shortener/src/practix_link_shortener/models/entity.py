"""ORM-отражение схемы коротких ссылок.

Модель повторяет сырой DDL из ``migrations/versions/0001_initial_shortener_schema.py``
дословно, включая имена ограничений: схему применяет миграция, а не
``create_all``, и расхождение между ними означало бы, что тесты проверяют не ту
таблицу, которая поедет в продакшен.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from practix_link_shortener.models.base import Base

#: Обычный редирект: резолвим код и уводим. Ничего не подтверждает.
KIND_REDIRECT = 'redirect'
#: Подтверждение адреса: перед редиректом зовём Auth. Требует ``user_id``.
KIND_CONFIRM_EMAIL = 'confirm_email'

KINDS = (KIND_REDIRECT, KIND_CONFIRM_EMAIL)


class ShortLink(Base):
    """Короткая ссылка.

    Код — САМ первичный ключ, а не суррогатный ``id`` с уникальным индексом
    поверх: на таблицу никто не ссылается, а каждое чтение — это ``WHERE
    code = ?``. Второй B-tree стоил бы записи и не окупился бы ни разу.
    """

    __tablename__ = 'short_link'
    __table_args__ = (
        CheckConstraint(f'kind IN ({", ".join(repr(k) for k in KINDS)})', name='short_link_kind_check'),
        # Ссылка подтверждения без пользователя ничего не подтверждает — это не
        # «неполные данные», а бессмысленная строка. Пусть падает на записи.
        CheckConstraint("kind <> 'confirm_email' OR user_id IS NOT NULL", name='short_link_confirm_needs_user'),
    )

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text(f"'{KIND_REDIRECT}'"))
    #: Тот самый «уникальный идентификатор пользователя, чтобы считать визиты» из
    #: задания. Для kind='redirect' пуст — обычная ссылка ничья.
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: Он же `redirectUrl` из задания: куда увести после (для confirm_email —
    #: после успешного подтверждения).
    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    #: Ключ идемпотентности вызывающего. Повторная сборка пачки писем не должна
    #: порождать вторую ссылку тому же человеку — иначе два письма разошлись бы
    #: разными адресами, и счётчик визитов размазался бы по двум строкам.
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # timestamptz, а не naive DateTime как в Auth: срок годности сравнивается с
    # now() из другого контейнера, и naive сделал бы корректность заложницей
    # локальной зоны сервера.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: БЕЗ индекса — намеренно: инкремент на каждый переход обязан оставаться
    #: HOT-обновлением, а индекс по изменяемой колонке это ломает.
    visit_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text('0'))
    first_visited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_visited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
