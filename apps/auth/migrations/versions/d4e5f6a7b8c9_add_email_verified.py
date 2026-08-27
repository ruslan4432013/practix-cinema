"""Add email_verified flag to users

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-08-05 15:00:00.000000

Признак подтверждённого адреса. Ставит его не Auth сам по себе, а переход
пользователя по короткой ссылке из welcome-письма: сервис сокращения ссылок
резолвит код и зовёт ``POST /api/v1/users/{user_id}/confirm-email``. Владелец
флага — Auth, потому что он владеет пользователем; шортенер в auth-db не пишет.

Миграция написана руками, а не autogenerate: тот сравнивает модели со схемой
целиком и на секционированной ``login_history`` каждый раз предлагает снести и
пересоздать секции.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: str | Sequence[str] | None = 'c3d4e5f6a7b8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # NOT NULL DEFAULT false, а не nullable: «неизвестно, подтверждён ли адрес» —
    # это не состояние, а дыра. Все заведённые до этой версии адреса не
    # подтверждены, и false говорит это прямо. На PostgreSQL >= 11 добавление
    # такой колонки не переписывает таблицу.
    op.add_column(
        'users',
        sa.Column('email_verified', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # А вот момент подтверждения nullable честно: у неподтверждённого его нет.
    op.add_column('users', sa.Column('email_verified_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'email_verified_at')
    op.drop_column('users', 'email_verified')
