-- Схема-эталон для сравнения. Применяется автоматически при первом старте
-- контейнера (монтирование в /docker-entrypoint-initdb.d).
--
-- Здесь требование задания «заложить хранение smallint» выполняется буквально:
-- это единственное из трёх хранилищ, где такой тип есть как тип, а не как
-- ограничение диапазона.

-- --- Лайки -----------------------------------------------------------------
-- Первичный ключ (user_id, film_id) — это и есть требование «один пользователь
-- ставит фильму одну оценку». Он же обслуживает чтение «лайки пользователя».
CREATE TABLE likes (
    user_id    uuid        NOT NULL,
    film_id    uuid        NOT NULL,
    rating     smallint    NOT NULL CHECK (rating BETWEEN 0 AND 10),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, film_id)
);

-- Второй паттерн доступа — «по фильму». Без отдельного индекса он превращается
-- в seq scan по 10 млн строк, и это ровно та разница, которую меряет бенчмарк.
CREATE INDEX likes_film_rating_idx ON likes (film_id, rating);

-- Правило ESR в терминах PostgreSQL: равенство по user_id, дальше поля в том
-- порядке, в котором идёт сортировка. Первичный ключ (user_id, film_id) выборку
-- «понравившееся» не закрывает — по нему пришлось бы сортировать вручную.
CREATE INDEX likes_user_rating_idx ON likes (user_id, rating DESC, updated_at DESC);

-- --- Преагрегат рейтинга фильма --------------------------------------------
-- hist[i] — число оценок со значением i-1 (массивы в PostgreSQL 1-индексные).
-- Гистограмма, а не пара счётчиков «лайки/дизлайки»: порог, по которому оценка
-- считается лайком, — продуктовое решение аналитиков и будет меняться. Из 11
-- чисел любой порог считается на лету, а два счётчика пришлось бы пересчитывать
-- по всей таблице.
CREATE TABLE film_rating (
    film_id       uuid   PRIMARY KEY,
    ratings_count integer NOT NULL DEFAULT 0,
    ratings_sum   bigint  NOT NULL DEFAULT 0,
    hist          integer[] NOT NULL DEFAULT array_fill(0, ARRAY[11])
);

-- --- Закладки ---------------------------------------------------------------
CREATE TABLE bookmarks (
    user_id    uuid        NOT NULL,
    film_id    uuid        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, film_id)
);

CREATE INDEX bookmarks_user_created_idx ON bookmarks (user_id, created_at DESC);

-- --- Рецензии ---------------------------------------------------------------
-- votes_* и useful_score денормализованы в саму рецензию: сортировка списка по
-- полезности не должна джойнить таблицу голосов на 2 млн строк.
CREATE TABLE reviews (
    review_id      uuid        PRIMARY KEY,
    film_id        uuid        NOT NULL,
    user_id        uuid        NOT NULL,
    body           text        NOT NULL,
    author_rating  smallint    CHECK (author_rating BETWEEN 0 AND 10),
    created_at     timestamptz NOT NULL,
    votes_likes    integer     NOT NULL DEFAULT 0,
    votes_dislikes integer     NOT NULL DEFAULT 0,
    useful_score   integer     NOT NULL DEFAULT 0
);

-- По индексу на каждый поддерживаемый порядок сортировки. Добавление нового
-- алгоритма ранжирования стоит одного индекса, а не переезда схемы.
CREATE INDEX reviews_film_created_idx ON reviews (film_id, created_at DESC);
CREATE INDEX reviews_film_useful_idx ON reviews (film_id, useful_score DESC);
CREATE INDEX reviews_film_author_rating_idx ON reviews (film_id, author_rating DESC);

-- --- Голоса за рецензии -----------------------------------------------------
CREATE TABLE review_votes (
    review_id  uuid        NOT NULL,
    user_id    uuid        NOT NULL,
    value      smallint    NOT NULL CHECK (value IN (-1, 1)),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (review_id, user_id)
);
