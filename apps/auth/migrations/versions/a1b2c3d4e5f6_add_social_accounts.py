"""Add social_accounts table and make users.password nullable

Revision ID: a1b2c3d4e5f6
Revises: 276725a5c545
Create Date: 2026-07-01 22:40:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: str | Sequence[str] | None = '276725a5c545'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Пользователи, вошедшие через OAuth, не имеют пароля.
    op.alter_column('users', 'password',
                    existing_type=sa.String(length=255),
                    nullable=True)
    op.create_table('social_accounts',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('provider', sa.String(length=50), nullable=False),
    sa.Column('provider_user_id', sa.String(length=255), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('provider', 'provider_user_id', name='uq_social_provider_user')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('social_accounts')
    op.alter_column('users', 'password',
                    existing_type=sa.String(length=255),
                    nullable=False)
