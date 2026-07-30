-- Витрины, отвечающие на два вопроса задания.
--
-- Все счётчики построены на uniq*State, а не на count*State. Причина не в
-- эстетике: материализованное представление срабатывает на ВСТАВЛЯЕМЫЙ блок, а
-- не на слитые данные, поэтому повторно доставленное событие попало бы в
-- count дважды, и отложенная дедупликация ReplacingMergeTree этого уже не
-- исправила бы. uniq по стабильному ключу к дублям невосприимчив.

-- =========================================================================
-- Витрина 1: самые просматриваемые фильмы.
-- =========================================================================
CREATE TABLE IF NOT EXISTS ugc.film_views_daily_local ON CLUSTER '{cluster}'
(
    view_date       Date,
    film_id         UUID,
    views           AggregateFunction(uniq, String),
    viewers         AggregateFunction(uniq, String),
    watched_ms      AggregateFunction(sum, UInt64),
    avg_completion  AggregateFunction(avgIf, Float32, UInt8)
)
ENGINE = ReplicatedAggregatingMergeTree
PARTITION BY toYYYYMM(view_date)
ORDER BY (film_id, view_date);

CREATE MATERIALIZED VIEW IF NOT EXISTS ugc.mv_film_views_daily ON CLUSTER '{cluster}'
TO ugc.film_views_daily_local AS
SELECT
    toDate(event_time)                                                  AS view_date,
    film_id,
    -- Просмотр = сеанс, а не событие: иначе двухчасовой фильм с тиком раз в
    -- 30 секунд «набирал» бы 240 просмотров на одного зрителя.
    uniqState(view_id)                                                  AS views,
    -- Зритель: авторизованный — по user_id, аноним — по сессии.
    -- ifNull, а не if(... IS NULL, ...): toString(Nullable(UUID)) даёт
    -- Nullable(String), и состояние агрегата не совпало бы по типу с колонкой
    -- целевой таблицы (AggregateFunction(uniq, String)).
    uniqState(ifNull(toString(user_id), session_id))                     AS viewers,
    sumState(toUInt64(watched_ms))                                      AS watched_ms,
    -- Средняя доля досмотра считается только по терминальным событиям:
    -- у метки прогресса completion_rate — это «докуда дошёл на текущий момент»,
    -- и усреднение с ними занижало бы показатель на длину фильма.
    avgIfState(completion_rate, toUInt8(event_type = 'video_completed')) AS avg_completion
FROM ugc.film_views_local
GROUP BY view_date, film_id;

CREATE TABLE IF NOT EXISTS ugc.film_views_daily ON CLUSTER '{cluster}'
AS ugc.film_views_daily_local
ENGINE = Distributed('{cluster}', ugc, film_views_daily_local, cityHash64(film_id));


-- =========================================================================
-- Витрина 2: кривая досмотра — где именно бросают фильм.
-- =========================================================================
CREATE TABLE IF NOT EXISTS ugc.film_retention_daily_local ON CLUSTER '{cluster}'
(
    view_date    Date,
    film_id      UUID,
    progress_pct UInt8,
    views        AggregateFunction(uniq, String)
)
ENGINE = ReplicatedAggregatingMergeTree
PARTITION BY toYYYYMM(view_date)
ORDER BY (film_id, view_date, progress_pct);

CREATE MATERIALIZED VIEW IF NOT EXISTS ugc.mv_film_retention_daily ON CLUSTER '{cluster}'
TO ugc.film_retention_daily_local AS
SELECT
    toDate(event_time) AS view_date,
    film_id,
    progress_pct,
    uniqState(view_id) AS views
FROM ugc.film_views_local
-- Только метки прогресса. Смена качества не говорит о том, докуда досмотрели
-- (она случается один раз в произвольной точке), а video_completed всегда
-- попадает в верхние бакеты и задрал бы правый край кривой.
WHERE event_type = 'video_progress' AND duration_ms > 0
GROUP BY view_date, film_id, progress_pct;

CREATE TABLE IF NOT EXISTS ugc.film_retention_daily ON CLUSTER '{cluster}'
AS ugc.film_retention_daily_local
ENGINE = Distributed('{cluster}', ugc, film_retention_daily_local, cityHash64(film_id));


-- =========================================================================
-- Витрина 3: доля брошенных просмотров и распределение досмотра.
-- =========================================================================
CREATE TABLE IF NOT EXISTS ugc.film_completion_daily_local ON CLUSTER '{cluster}'
(
    view_date    Date,
    film_id      UUID,
    starts       AggregateFunction(uniqIf, String, UInt8),
    finishes     AggregateFunction(uniqIf, String, UInt8),
    q_completion AggregateFunction(quantiles(0.25, 0.5, 0.9), Float32)
)
ENGINE = ReplicatedAggregatingMergeTree
PARTITION BY toYYYYMM(view_date)
ORDER BY (film_id, view_date);

CREATE MATERIALIZED VIEW IF NOT EXISTS ugc.mv_film_completion_daily ON CLUSTER '{cluster}'
TO ugc.film_completion_daily_local AS
SELECT
    toDate(event_time) AS view_date,
    film_id,
    -- «Начал смотреть» = есть метка в первых 5 % длительности. Считать началом
    -- любое событие нельзя: тогда зритель, подключившийся к середине, попал бы
    -- в знаменатель воронки наравне с начавшим сначала.
    uniqIfState(view_id, toUInt8(progress_pct <= 5))    AS starts,
    -- «Досмотрел» = дошёл до 90 %. Требовать 100 % значит объявить
    -- недосмотренным почти каждый просмотр: последние 10 % — это титры.
    uniqIfState(view_id, toUInt8(completion_rate >= 0.9)) AS finishes,
    quantilesState(0.25, 0.5, 0.9)(completion_rate)     AS q_completion
FROM ugc.film_views_local
WHERE duration_ms > 0
GROUP BY view_date, film_id;

CREATE TABLE IF NOT EXISTS ugc.film_completion_daily ON CLUSTER '{cluster}'
AS ugc.film_completion_daily_local
ENGINE = Distributed('{cluster}', ugc, film_completion_daily_local, cityHash64(film_id));
