"""Начальная схема UGC: оценки, преагрегат рейтинга, закладки, рецензии, голоса.

Это перенос ``research/ugc-storage/config/postgres/01_schema.sql`` — той самой
схемы, на которой получены замеры в ``research/ugc-storage/README.md``. Отход от
неё обесценивает эти замеры, поэтому имена индексов сохранены дословно, а
комментарии «зачем этот индекс» перенесены сюда: причина не должна остаться
только в каталоге исследования.

Миграция написана РУЧНЫМ DDL, а не autogenerate, и это не лень наоборот:

* ``hist integer[] NOT NULL DEFAULT array_fill(0, ARRAY[11])`` — значение по
  умолчанию в виде вызова функции. Autogenerate вообще не сравнивает server
  default'ы без ``compare_server_default=True``, а с ним сравнивает текст
  выражения и выдаёт вечный фантомный diff.
* CHECK-ограничения autogenerate не генерирует. А это они гарантируют, что в
  ``hist`` не придёт индекс за пределами 0..10.
* Индексы с ``DESC`` для autogenerate — выражения; он не умеет их сравнивать и
  предлагал бы пересоздавать их при каждом запуске.

Revision ID: 0001_initial_ugc
"""

from alembic import op

revision = '0001_initial_ugc'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- Оценки -------------------------------------------------------------
    # Первичный ключ (user_id, film_id) — это и есть требование «один
    # пользователь ставит фильму одну оценку». Он же обслуживает чтение
    # «оценки пользователя».
    op.execute(
        """
        CREATE TABLE likes (
            user_id    uuid        NOT NULL,
            film_id    uuid        NOT NULL,
            rating     smallint    NOT NULL CONSTRAINT likes_rating_check CHECK (rating BETWEEN 0 AND 10),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, film_id)
        )
        """
    )
    # Второй паттерн доступа — «по фильму». Без отдельного индекса он
    # превращается в seq scan по 10 млн строк, и это ровно та разница, которую
    # измерял бенчмарк.
    op.execute('CREATE INDEX likes_film_rating_idx ON likes (film_id, rating)')
    # Правило ESR в терминах PostgreSQL: равенство по user_id, дальше поля в том
    # порядке, в котором идёт сортировка.
    op.execute('CREATE INDEX likes_user_rating_idx ON likes (user_id, rating DESC, updated_at DESC)')

    # --- Преагрегат рейтинга фильма ----------------------------------------
    # hist[i] — число оценок со значением i-1 (массивы в PostgreSQL 1-индексные).
    # Гистограмма, а не пара счётчиков «лайки/дизлайки»: порог, по которому
    # оценка считается лайком, — продуктовое решение и будет меняться. Из 11
    # чисел любой порог считается на лету, а два счётчика пришлось бы
    # пересчитывать по всей таблице.
    op.execute(
        """
        CREATE TABLE film_rating (
            film_id       uuid      PRIMARY KEY,
            ratings_count integer   NOT NULL DEFAULT 0,
            ratings_sum   bigint    NOT NULL DEFAULT 0,
            hist          integer[] NOT NULL DEFAULT array_fill(0, ARRAY[11])
        )
        """
    )

    # --- Закладки -----------------------------------------------------------
    op.execute(
        """
        CREATE TABLE bookmarks (
            user_id    uuid        NOT NULL,
            film_id    uuid        NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, film_id)
        )
        """
    )
    op.execute('CREATE INDEX bookmarks_user_created_idx ON bookmarks (user_id, created_at DESC)')

    # --- Рецензии -----------------------------------------------------------
    # votes_* и useful_score денормализованы в саму рецензию: сортировка списка
    # по полезности не должна джойнить таблицу голосов на 2 млн строк.
    op.execute(
        """
        CREATE TABLE reviews (
            review_id      uuid        PRIMARY KEY,
            film_id        uuid        NOT NULL,
            user_id        uuid        NOT NULL,
            body           text        NOT NULL,
            author_rating  smallint    CONSTRAINT reviews_author_rating_check CHECK (author_rating BETWEEN 0 AND 10),
            created_at     timestamptz NOT NULL DEFAULT now(),
            votes_likes    integer     NOT NULL DEFAULT 0,
            votes_dislikes integer     NOT NULL DEFAULT 0,
            useful_score   integer     NOT NULL DEFAULT 0
        )
        """
    )
    # ЕДИНСТВЕННОЕ ДОБАВЛЕНИЕ К ИЗМЕРЕННОЙ СХЕМЕ. Продуктовое правило «одна
    # рецензия пользователя на фильм» без уникального индекса не выразить:
    # проверка «сначала посмотрел, потом вставил» состязательна. В стенде
    # ограничения не было, потому что там рецензии только заливались пачками.
    # Цена — один индекс на записи; измеренные пути чтения он не трогает.
    op.execute('CREATE UNIQUE INDEX reviews_film_user_uniq ON reviews (film_id, user_id)')
    # По индексу на каждый поддерживаемый порядок сортировки. Добавление нового
    # алгоритма ранжирования стоит одного индекса, а не переезда схемы.
    op.execute('CREATE INDEX reviews_film_created_idx ON reviews (film_id, created_at DESC)')
    op.execute('CREATE INDEX reviews_film_useful_idx ON reviews (film_id, useful_score DESC)')
    # NULLS LAST — второе и последнее отступление: author_rating необязателен, а
    # PostgreSQL при DESC ставит NULL первыми, и рецензии без оценки автора
    # возглавили бы список. Порядок в индексе обязан совпадать с порядком в
    # запросе, иначе индекс перестанет использоваться.
    op.execute('CREATE INDEX reviews_film_author_rating_idx ON reviews (film_id, author_rating DESC NULLS LAST)')

    # --- Голоса за рецензии -------------------------------------------------
    op.execute(
        """
        CREATE TABLE review_votes (
            review_id  uuid        NOT NULL,
            user_id    uuid        NOT NULL,
            value      smallint    NOT NULL CONSTRAINT review_votes_value_check CHECK (value IN (-1, 1)),
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (review_id, user_id)
        )
        """
    )


def downgrade() -> None:
    op.execute('DROP TABLE IF EXISTS review_votes')
    op.execute('DROP TABLE IF EXISTS reviews')
    op.execute('DROP TABLE IF EXISTS bookmarks')
    op.execute('DROP TABLE IF EXISTS film_rating')
    op.execute('DROP TABLE IF EXISTS likes')
