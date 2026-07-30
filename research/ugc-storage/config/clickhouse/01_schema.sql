-- Схема-эталон для сравнения. Применяется при первом старте контейнера
-- (монтирование в /docker-entrypoint-initdb.d).
--
-- В проекте ClickHouse уже стоит и обслуживает аналитику (apps/etl-clickhouse),
-- поэтому «взять его же и под UGC» — очевидная гипотеза, которую нужно не
-- отвергнуть на словах, а измерить.
--
-- Тип оценки — UInt8. Прямой аналог smallint здесь Int16, но диапазон 0..10 не
-- требует 16 бит; требование задания «не булево, а число» соблюдено.

-- --- Лайки -----------------------------------------------------------------
-- Изменение оценки в ClickHouse — это не UPDATE, а вставка новой версии;
-- ReplacingMergeTree схлопывает версии при слиянии. Когда слияние произойдёт —
-- не определено, поэтому любое честное чтение обязано доагрегировать версии
-- само (argMax по updated_at). Это и есть цена LSM-подхода на точечных данных.
CREATE TABLE IF NOT EXISTS ugc.likes
(
    film_id    UUID,
    user_id    UUID,
    rating     UInt8,
    created_at DateTime64(3),
    updated_at DateTime64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (film_id, user_id);

-- Ключ сортировки обслуживает ровно один паттерн доступа. Чтобы чтение «лайки
-- пользователя» не превращалось в full scan, нужна вторая копия данных с другим
-- порядком — это стандартный приём и одновременно честная цена: двойной объём и
-- двойная запись.
CREATE TABLE IF NOT EXISTS ugc.likes_by_user
(
    user_id    UUID,
    rating     UInt8,
    film_id    UUID,
    created_at DateTime64(3),
    updated_at DateTime64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (user_id, film_id);

CREATE MATERIALIZED VIEW IF NOT EXISTS ugc.mv_likes_by_user TO ugc.likes_by_user AS
SELECT user_id, rating, film_id, created_at, updated_at
FROM ugc.likes;

-- --- Преагрегат рейтинга фильма --------------------------------------------
-- hist[i] — число оценок со значением i-1 (массивы в ClickHouse 1-индексные,
-- как и в PostgreSQL). Гистограмма вместо пары счётчиков — чтобы порог «что
-- считать лайком» можно было менять без пересчёта по всей таблице.
--
-- Движок SummingMergeTree, а не Replacing: он складывает числовые колонки при
-- слиянии, причём массивы — поэлементно. Благодаря этому изменение оценки — это
-- вставка дельты, а не «прочитать, изменить, записать». Read-modify-write в
-- ClickHouse не только медленнее в три обращения, но и попросту небезопасен:
-- изолировать его нечем.
CREATE TABLE IF NOT EXISTS ugc.film_rating
(
    film_id       UUID,
    ratings_count Int64,
    ratings_sum   Int64,
    hist          Array(Int64)
)
ENGINE = SummingMergeTree
ORDER BY film_id;

-- --- Закладки ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ugc.bookmarks
(
    user_id    UUID,
    film_id    UUID,
    created_at DateTime64(3)
)
ENGINE = ReplacingMergeTree(created_at)
ORDER BY (user_id, film_id);

-- --- Рецензии ---------------------------------------------------------------
-- Отдельных индексов под сортировки нет и быть не может: у таблицы один ключ
-- сортировки. Сортировка по полезности или по оценке автора — это сортировка
-- уже отобранного по film_id куска (десятки строк), что дёшево, но выбор
-- порядка не ускоряется структурой, как в B-tree.
CREATE TABLE IF NOT EXISTS ugc.reviews
(
    film_id        UUID,
    review_id      UUID,
    user_id        UUID,
    body           String,
    author_rating  UInt8,
    created_at     DateTime64(3),
    votes_likes    UInt32,
    votes_dislikes UInt32,
    useful_score   Int32,
    updated_at     DateTime64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (film_id, review_id);

-- --- Голоса за рецензии -----------------------------------------------------
CREATE TABLE IF NOT EXISTS ugc.review_votes
(
    review_id  UUID,
    user_id    UUID,
    value      Int8,
    created_at DateTime64(3)
)
ENGINE = ReplacingMergeTree(created_at)
ORDER BY (review_id, user_id);
