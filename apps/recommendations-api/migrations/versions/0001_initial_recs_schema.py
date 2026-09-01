"""Начальная схема витрины рекомендаций.

Миграция написана РУЧНЫМ DDL, а не autogenerate, по тем же причинам, что и
начальные схемы UGC и шортенера:

* CHECK-ограничения autogenerate не генерирует вовсе. А здесь именно они
  делают «указатель — ровно одна строка» свойством схемы, а не договорённостью.
* Составной первичный ключ, начинающийся с ``version``, — решение, которое
  хочется видеть в тексте DDL: из него следуют и идемпотентность прогона
  (F0.1), и целостность чтения (F0.2), и оно же задаёт физический порядок
  строк, при котором выдача одного фильма читается одним диапазоном индекса.
* ``ON DELETE RESTRICT`` у указателя: уборка старых версий не имеет права
  снести ту, на которую сейчас смотрит выдача.

Revision ID: 0001_initial_recs
"""

from alembic import op

revision = '0001_initial_recs'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Версия батча. run_key — естественный ключ прогона ('train:2026-08-30T00'),
    # и его UNIQUE — единственное, что стоит между двумя тиками планировщика и
    # второй витриной на тех же данных. Тот же приём, что у scheduled_run.run_key
    # в нотификациях: идемпотентность держится на ограничении СУБД, а не на том,
    # что вызывающий не повторится.
    op.execute(
        """
        CREATE TABLE shelf_version (
            version     bigserial   PRIMARY KEY,
            run_key     varchar(128) NOT NULL UNIQUE,
            status      varchar(16) NOT NULL DEFAULT 'building',
            models      varchar(64) NOT NULL DEFAULT '',
            started_at  timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz NULL,
            stats       jsonb       NOT NULL DEFAULT '{}'::jsonb,
            CONSTRAINT shelf_version_status_check
                CHECK (status IN ('building', 'ready', 'failed'))
        )
        """
    )
    # По этому индексу считается возраст витрины (F0.4) — самый частый служебный
    # запрос: «когда закончился последний успешный прогон».
    op.execute('CREATE INDEX shelf_version_finished_idx ON shelf_version (finished_at DESC NULLS LAST)')

    # Указатель на актуальную версию. `id boolean PRIMARY KEY CHECK (id)` — это
    # «ровно одна строка», выраженное схемой: второй INSERT упрётся в первичный
    # ключ, а id = false не пройдёт CHECK. Договорённость «всегда пишем
    # WHERE id = 1» держалась бы ровно до первой ошибки в чужом коде.
    #
    # version NULL — законное состояние: стенд поднялся, обучение ещё не
    # проходило. Выдача в этот момент обязана отвечать 200 с пустым популярным,
    # а не 500 (F1.3).
    op.execute(
        """
        CREATE TABLE shelf_pointer (
            id          boolean     PRIMARY KEY DEFAULT true,
            version     bigint      NULL REFERENCES shelf_version (version) ON DELETE RESTRICT,
            switched_at timestamptz NULL,
            CONSTRAINT shelf_pointer_single_row_check CHECK (id)
        )
        """
    )
    op.execute('INSERT INTO shelf_pointer (id, version) VALUES (true, NULL)')

    # Три вида списков — один формат: (версия, ключ, позиция) -> (фильм, вес).
    # Это и есть шов между офлайном и онлайном из раздела 5.4 ТЗ: смена модели
    # за этим контрактом не трогает ни API, ни nginx, ни фронт.
    #
    # rank в первичном ключе, а не отдельная сортировка по score: порядок
    # посчитан батчем один раз, и выдаче незачем сортировать его заново на
    # каждом запросе. ORDER BY rank идёт по индексу.
    op.execute(
        """
        CREATE TABLE similar_item (
            version     bigint   NOT NULL,
            film_id     uuid     NOT NULL,
            rank        smallint NOT NULL,
            rec_film_id uuid     NOT NULL,
            score       real     NOT NULL,
            PRIMARY KEY (version, film_id, rank)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE personal_item (
            version bigint   NOT NULL,
            user_id uuid     NOT NULL,
            rank    smallint NOT NULL,
            film_id uuid     NOT NULL,
            score   real     NOT NULL,
            PRIMARY KEY (version, user_id, rank)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE popular_item (
            version bigint   NOT NULL,
            rank    smallint NOT NULL,
            film_id uuid     NOT NULL,
            score   real     NOT NULL,
            PRIMARY KEY (version, rank)
        )
        """
    )

    # Снимок идентификаторов каталога на момент обучения. Только id: каталог не
    # дублируется (F2.5), карточку отдаёт Movies API. Нужен, чтобы отличить
    # «фильма нет» (404) от «фильм есть, соседей нет» (200 с популярным) и
    # чтобы посчитать покрытие каталога.
    op.execute(
        """
        CREATE TABLE catalog_film (
            version bigint NOT NULL,
            film_id uuid   NOT NULL,
            PRIMARY KEY (version, film_id)
        )
        """
    )


def downgrade() -> None:
    op.execute('DROP TABLE IF EXISTS catalog_film')
    op.execute('DROP TABLE IF EXISTS popular_item')
    op.execute('DROP TABLE IF EXISTS personal_item')
    op.execute('DROP TABLE IF EXISTS similar_item')
    op.execute('DROP TABLE IF EXISTS shelf_pointer')
    op.execute('DROP TABLE IF EXISTS shelf_version')
