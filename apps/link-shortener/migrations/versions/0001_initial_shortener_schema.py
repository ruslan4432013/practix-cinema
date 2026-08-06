"""Начальная схема сервиса сокращения ссылок: одна таблица short_link.

Миграция написана РУЧНЫМ DDL, а не autogenerate, по тем же причинам, что и
начальная схема UGC:

* CHECK-ограничения autogenerate не генерирует вовсе. А это они запрещают
  завести ссылку подтверждения без пользователя — строку, которая ничего не
  подтверждает.
* Частичный уникальный индекс (``WHERE idempotency_key IS NOT NULL``)
  autogenerate тоже не воспроизводит.
* Первичный ключ по ``code`` вместо суррогатного ``id`` — решение, которое
  хочется видеть в тексте DDL, а не выводить из модели.

Revision ID: 0001_initial_shortener
"""

from alembic import op

revision = '0001_initial_shortener'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `code` — И ЕСТЬ первичный ключ. Суррогатный id + UNIQUE(code) дал бы два
    # B-tree на таблицу, у которой единственный паттерн чтения — `WHERE code = ?`
    # и на которую никто не ссылается внешним ключом.
    #
    # varchar(16), а не char(7): длина кода — настройка (SHORTENER_CODE_LENGTH),
    # а char(n) в PostgreSQL добивает значение пробелами до полной длины.
    #
    # Все временные метки timestamptz. Срок годности сравнивается с now() из
    # чужого контейнера; naive-колонка (как users.created_at в Auth) сделала бы
    # корректность заложницей локальной зоны сервера.
    op.execute(
        """
        CREATE TABLE short_link (
            code             varchar(16)  PRIMARY KEY,
            kind             varchar(32)  NOT NULL DEFAULT 'redirect',
            user_id          uuid         NULL,
            target_url       text         NOT NULL,
            idempotency_key  varchar(64)  NULL,
            created_at       timestamptz  NOT NULL DEFAULT now(),
            expires_at       timestamptz  NOT NULL,
            revoked_at       timestamptz  NULL,
            visit_count      bigint       NOT NULL DEFAULT 0,
            first_visited_at timestamptz  NULL,
            last_visited_at  timestamptz  NULL,
            CONSTRAINT short_link_kind_check
                CHECK (kind IN ('redirect', 'confirm_email')),
            CONSTRAINT short_link_confirm_needs_user
                CHECK (kind <> 'confirm_email' OR user_id IS NOT NULL)
        )
        """
    )

    # Идемпотентность вызывающего. PostgreSQL и так считает NULL'ы различными,
    # но частичный индекс меньше и прямо заявляет намерение: уникальны только
    # заданные ключи. ИМЯ ограничения важно: сервис различает по нему две
    # причины IntegrityError — коллизию кода (перегенерировать) и повтор ключа
    # идемпотентности (вернуть существующую строку).
    op.execute(
        'CREATE UNIQUE INDEX short_link_idempotency_key_uq ON short_link (idempotency_key) '
        'WHERE idempotency_key IS NOT NULL'
    )
    # «Все ссылки пользователя» — для разбора инцидента и для отзыва скопом.
    op.execute('CREATE INDEX short_link_user_id_idx ON short_link (user_id) WHERE user_id IS NOT NULL')
    # Для уборщика (`cli purge`), а НЕ для горячего пути: тот ходит по PK.
    op.execute('CREATE INDEX short_link_expires_at_idx ON short_link (expires_at)')

    # Индекса по visit_count нет НАМЕРЕННО: инкремент на каждый переход должен
    # оставаться HOT-обновлением, а индекс по изменяемой колонке это ломает —
    # каждое обновление начало бы плодить записи в индексе и раздувать его.


def downgrade() -> None:
    op.execute('DROP TABLE IF EXISTS short_link')
