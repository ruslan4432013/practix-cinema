-- Типизированная таблица просмотров фильмов.
--
-- Заполняется из ДВУХ типов событий: video_progress (метки прогресса) и
-- video_completed (досмотр). Всё, что нужно для обоих вопросов задания —
-- «самые просматриваемые фильмы» и «какие фильмы не досматривают» — лежит
-- здесь в разобранном виде, без парсинга JSON на каждый запрос.
--
-- video_quality_change сюда не пишется, хотя тоже относится к фильму: у него
-- нет duration_ms, и completion_rate/progress_pct получались бы нулевыми — не
-- «ноль прогресса», а «прогресс неизвестен». Такие строки смещали бы к нулю
-- любое среднее по прогрессу, посчитанное без фильтра по event_type. Смена
-- качества целиком доступна в ugc.raw_events (см. docs/analytics_schema.md).

CREATE TABLE IF NOT EXISTS ugc.film_views_local ON CLUSTER '{cluster}'
(
    event_id             UUID,
    event_type           LowCardinality(String),
    film_id              UUID,
    user_id              Nullable(UUID),
    session_id           String,
    -- Идентификатор одного СЕАНСА просмотра (session_id:film_id).
    -- По нему считаются просмотры, а не события: метки прогресса идут
    -- десятками на один просмотр, и count() по ним измерял бы длину фильма,
    -- а не его популярность. Побочный, но важный эффект: uniq(view_id)
    -- невосприимчив к повторной доставке по построению.
    view_id              String,
    -- coalesce(event_timestamp, received_at): предпочитаем клиентское время
    -- (оно точнее отражает момент действия), но всегда имеем чем его заменить.
    event_time           DateTime64(3, 'UTC'),
    received_at          DateTime64(3, 'UTC'),
    playback_position_ms UInt32,
    duration_ms          UInt32,
    watched_ms           UInt32,
    -- 0..1, считает сервер-коллектор из длительностей. Присланному клиентом
    -- проценту доверять нельзя.
    completion_rate      Float32,
    -- Бакет прогресса с шагом 5 %: по нему строится кривая досмотра.
    -- Округление сделано на этапе ETL, а не в запросе, — иначе каждый запрос
    -- аналитика пересчитывал бы его по всей таблице.
    progress_pct         UInt8,
    quality              LowCardinality(String),
    device_type          LowCardinality(String),
    is_authenticated     UInt8,
    ingested_at          DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC')
)
ENGINE = ReplicatedReplacingMergeTree(ingested_at)
PARTITION BY toYYYYMM(event_time)
-- film_id первым: фильтр и группировка по фильму есть в обоих аналитических
-- запросах. Дата второй — для отсечения диапазона. event_id третьим — ключ
-- дедупликации.
ORDER BY (film_id, toDate(event_time), event_id)
TTL toDateTime(event_time) + INTERVAL 24 MONTH DELETE
SETTINGS index_granularity = 8192;

-- Шардирование по film_id — прямое следствие обоих вопросов задания: и «самые
-- просматриваемые», и «какие не досматривают» группируются по фильму. Все
-- строки фильма лежат на одном шарде, поэтому материализованные представления
-- считают на шарде ПОЛНЫЙ агрегат, а чтение через Distributed просто склеивает
-- независимые куски без пересылок. Перекос от блокбастера при двух шардах и
-- каталоге в сотни тысяч фильмов пренебрежимо мал.
CREATE TABLE IF NOT EXISTS ugc.film_views ON CLUSTER '{cluster}'
AS ugc.film_views_local
ENGINE = Distributed('{cluster}', ugc, film_views_local, cityHash64(film_id));
