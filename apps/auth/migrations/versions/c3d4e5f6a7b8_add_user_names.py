"""Add first_name and last_name to users

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-05 12:00:00.000000

Имя и фамилия нужны сервису нотификаций: воркер рассылки получает из очереди
только ``user_id`` и приходит за личными данными сюда, чтобы подставить их в
шаблон письма.

Миграция написана руками, а не autogenerate: тот сравнивает модели со схемой
целиком и на секционированной ``login_history`` каждый раз предлагает снести и
пересоздать секции.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: str | Sequence[str] | None = 'b2c3d4e5f6a7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # nullable без server_default: у всех существующих пользователей имени нет
    # физически, и NULL это честно показывает. Пустая строка выглядела бы как
    # «человек стёр своё имя», а рассылка на такое отреагировала бы иначе.
    op.add_column('users', sa.Column('first_name', sa.String(length=128), nullable=True))
    op.add_column('users', sa.Column('last_name', sa.String(length=128), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'last_name')
    op.drop_column('users', 'first_name')
