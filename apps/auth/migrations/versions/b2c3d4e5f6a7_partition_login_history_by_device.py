"""Partition login_history by device (composite RANGE(auth_date) -> LIST(device_type))

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-01 23:30:00.000000

Пересобирает login_history в двухуровневую секционированную таблицу:
верхний уровень — RANGE по auth_date (по годам), внутри года — LIST по
device_type (web/mobile/smart + DEFAULT-секция *_other). Первичный ключ
становится составным (id, auth_date, device_type), т.к. PostgreSQL требует
включать все столбцы ключа секционирования всех уровней.

Таблица пересоздаётся целиком: на login_history не ссылается ни один внешний
ключ, а это учебный проект без продовых данных.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: str | Sequence[str] | None = 'a1b2c3d4e5f6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Годы, для которых заранее создаются секции. 2025 — история из первой
# миграции; 2026/2027 — текущий и следующий год, чтобы вставки не падали на
# стыке года. Более далёкое будущее — задача для pg_partman.
_YEARS = (2025, 2026, 2027)
_DEVICES = ('web', 'mobile', 'smart')


def _create_year_partitions() -> None:
    for year in _YEARS:
        year_table = f'login_history_{year}'
        op.execute(f"""
            CREATE TABLE IF NOT EXISTS "{year_table}"
            PARTITION OF login_history
            FOR VALUES FROM ('{year}-01-01') TO ('{year + 1}-01-01')
            PARTITION BY LIST (device_type)
        """)
        for device in _DEVICES:
            op.execute(f"""
                CREATE TABLE IF NOT EXISTS "{year_table}_{device}"
                PARTITION OF "{year_table}" FOR VALUES IN ('{device}')
            """)
        op.execute(f"""
            CREATE TABLE IF NOT EXISTS "{year_table}_other"
            PARTITION OF "{year_table}" DEFAULT
        """)


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("DROP TABLE IF EXISTS login_history CASCADE")
    op.execute("""
        CREATE TABLE login_history (
            id uuid NOT NULL,
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            user_agent text,
            ip_address varchar(50),
            auth_date timestamp NOT NULL,
            device_type text NOT NULL DEFAULT 'web',
            PRIMARY KEY (id, auth_date, device_type)
        ) PARTITION BY RANGE (auth_date)
    """)
    _create_year_partitions()
    # Индекс объявляется на родительской таблице и наследуется всеми секциями;
    # обслуживает запрос истории входов конкретного пользователя.
    op.execute(
        "CREATE INDEX ix_login_history_user_date "
        "ON login_history (user_id, auth_date DESC)"
    )


def downgrade() -> None:
    """Downgrade schema — возврат к RANGE(auth_date) без device_type."""
    op.execute("DROP TABLE IF EXISTS login_history CASCADE")
    op.execute("""
        CREATE TABLE login_history (
            id uuid NOT NULL,
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            user_agent text,
            ip_address varchar(50),
            auth_date timestamp NOT NULL,
            PRIMARY KEY (id, auth_date)
        ) PARTITION BY RANGE (auth_date)
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS login_history_p2026
        PARTITION OF login_history
        FOR VALUES FROM ('2026-01-01') TO ('2027-01-01')
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS login_history_p2025
        PARTITION OF login_history
        FOR VALUES FROM ('2025-01-01') TO ('2026-01-01')
    """)
