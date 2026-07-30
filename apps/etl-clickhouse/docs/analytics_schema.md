# Схема аналитического хранилища

Документ для аналитика и для того, кто будет менять схему. Отвечает на три
вопроса: как устроены таблицы, почему именно так, и как правильно их читать.

## Карта

| Объект | Движок | Назначение |
|---|---|---|
| `ugc.raw_events` | Distributed → `ReplicatedReplacingMergeTree` | Сырой поток всех шести семейств событий |
| `ugc.film_views` | Distributed → `ReplicatedReplacingMergeTree` | Типизированные просмотры фильмов |
| `ugc.film_views_daily` | Distributed → `ReplicatedAggregatingMergeTree` | Витрина: топ просматриваемых |
| `ugc.film_retention_daily` | Distributed → `ReplicatedAggregatingMergeTree` | Витрина: кривая досмотра |
| `ugc.film_completion_daily` | Distributed → `ReplicatedAggregatingMergeTree` | Витрина: доля брошенных просмотров |
| `ugc.invalid_events` | Distributed → `ReplicatedMergeTree` | Карантин нераспознанных сообщений |

Витрины наполняются материализованными представлениями `mv_*` от
`film_views_local`. Писать в них напрямую не нужно и нельзя.

У каждой таблицы есть пара: `*_local` (данные на узле) и одноимённая
Distributed-обёртка. **Читать и писать нужно через обёртку**; `*_local`
существуют для DDL, `TRUNCATE` и разбора инцидентов.

## Ответы на вопросы задания

### 1. Самые просматриваемые фильмы

```sql
SELECT
    film_id,
    uniqMerge(views)                          AS views,
    uniqMerge(viewers)                        AS viewers,
    round(sumMerge(watched_ms) / 3600000., 1) AS watched_hours
FROM ugc.film_views_daily
WHERE view_date >= today() - 7
GROUP BY film_id
ORDER BY views DESC
LIMIT 20;
```

Ad-hoc-эквивалент прямо по детальным данным — `uniq` по `view_id`
невосприимчив к дублям, поэтому `FINAL` не нужен:

```sql
SELECT film_id, uniq(view_id) AS views
FROM ugc.film_views
WHERE event_time >= now() - INTERVAL 7 DAY
GROUP BY film_id ORDER BY views DESC LIMIT 20;
```

### 2. Какие фильмы не досматривают

```sql
SELECT
    film_id,
    uniqIfMerge(starts)                                       AS starts,
    uniqIfMerge(finishes)                                     AS finishes,
    round(1 - finishes / starts, 3)                           AS abandon_rate,
    round(quantilesMerge(0.25, 0.5, 0.9)(q_completion)[2], 3) AS median_completion
FROM ugc.film_completion_daily
WHERE view_date >= today() - 30
GROUP BY film_id
HAVING starts >= 100          -- отсечь шум на фильмах с единичными просмотрами
ORDER BY abandon_rate DESC
LIMIT 20;
```

> Внимание: у полей `starts` и `finishes` комбинатор `-If`, поэтому мержить их
> нужно `uniqIfMerge`, а не `uniqMerge`. Иначе ClickHouse вернёт
> `ILLEGAL_TYPE_OF_ARGUMENT`.

### 3. Где именно бросают конкретный фильм

```sql
SELECT
    progress_pct,
    uniqMerge(views)                    AS views,
    round(views / max(views) OVER (), 3) AS retention
FROM ugc.film_retention_daily
WHERE film_id = {film:UUID} AND view_date >= today() - 30
GROUP BY progress_pct
ORDER BY progress_pct;
```

Самое большое падение между соседними бакетами и есть точка выхода: пик на
`progress_pct = 10` означает затянутую завязку, пик на 95 — что просто
пропускают титры.

## Почему схема именно такая

### `view_id` — просмотр, а не событие

Метка прогресса приходит раз в 30 секунд, то есть двухчасовой фильм даёт около
240 событий на один просмотр. `count()` по событиям измерял бы **длину фильма,
а не его популярность**: длинный фильм с одним зрителем обошёл бы короткий с
десятью. Поэтому ETL проставляет `view_id = session_id:film_id`, и все витрины
считают `uniq(view_id)`.

Побочный, но не менее важный эффект: `uniq` по стабильному ключу невосприимчив
к повторной доставке — см. раздел про дедупликацию.

### `progress_pct` считается при загрузке

Бакет с шагом 5 % можно было бы вычислять в запросе, но тогда каждый запрос
аналитика пересчитывал бы его по всей таблице. Шаг 5 % — компромисс: 1 % даёт
шум и в двадцать раз больше строк в витрине, 10 % слишком грубо, чтобы
разглядеть точку выхода.

### В `film_views` только два типа событий

Витрина собирается из `video_progress` и `video_completed`. Смены качества
(`video_quality_change`) в ней нет намеренно: у события нет `duration_ms`, и
`completion_rate`/`progress_pct` для него получались бы нулями — а ноль здесь
означал бы «зритель не сдвинулся с начала», чего событие не утверждает. Такие
строки не влияли бы на `uniq(view_id)`, но смещали бы к нулю любое среднее по
прогрессу, посчитанное без явного `event_type != 'video_quality_change'`, —
ошибка, которую в дашборде не видно.

Смена качества полностью доступна в сырой таблице, где у неё есть и позиция, и
оба качества:

```sql
SELECT
    session_id,
    JSONExtractString(payload, 'film_id')             AS film_id,
    JSONExtractString(payload, 'from_quality')        AS from_quality,
    JSONExtractString(payload, 'to_quality')          AS to_quality,
    JSONExtractInt(payload, 'playback_position_ms')   AS position_ms,
    coalesce(event_timestamp, received_at)            AS event_time
FROM ugc.raw_events
WHERE event_type = 'video_quality_change'
  AND received_at >= now() - INTERVAL 1 DAY;
```

Типичный вопрос «бросают ли просмотр после переключения качества» решается
джойном такой выборки с `film_views` по `session_id` и `film_id`.

### Ключи сортировки

* `raw_events`: `(event_type, toDate(received_at), event_id)`. Тип события
  первым — он самый низкокардинальный и лучше всех сжимается; `event_id`
  последним, потому что он уникален и, стоя первым, испортил бы разрежённый
  индекс.
* `film_views`: `(film_id, toDate(event_time), event_id)`. Фильм первым, потому
  что фильтр и группировка по нему есть в обоих аналитических запросах.

### Ключи шардирования

* `raw_events` шардируется по `partition_key` (тот же ключ, по которому
  коллектор партиционировал сообщение в Kafka: `user_id → anonymous_id →
  session_id`). Все события одного пользователя лежат на одном шарде, поэтому
  сессионные воронки не требуют межшардовых пересылок. Шардировать сырой поток
  по `film_id` нельзя: у кликов, просмотров страниц и поисковых событий фильма
  нет вовсе.
* `film_views` и витрины шардируются по `film_id` — прямое следствие обоих
  вопросов задания. Материализованные представления считают на шарде **полный**
  агрегат, а чтение через Distributed просто склеивает независимые куски.
  Перекос от блокбастера при двух шардах и каталоге в сотни тысяч фильмов
  пренебрежимо мал.

### Сырой `payload` хранится строкой

Поле, которое сегодня никем не разобрано, завтра понадобится аналитику. А
перечитать Kafka через месяц уже нельзя — retention там 7–30 дней. Колонка сжата
`ZSTD(3)`; для разбора на лету годятся `JSONExtract*`.

## Дедупликация: три уровня

Доставка из Kafka гарантирована «хотя бы один раз». Дубликаты будут всегда:
коллектор ретраит отправку, дренаж буфера деградации ретраит её ещё раз, а сам
ETL может упасть между вставкой и коммитом оффсетов.

| Уровень | Механизм | Что закрывает |
|---|---|---|
| 1 | `set` по `event_id` внутри пачки в ETL | Повтор в пределах одной пачки |
| 2 | `insert_deduplication_token` из диапазонов оффсетов + штатная дедупликация блоков `Replicated*MergeTree` | Повторная вставка той же пачки после падения между вставкой и коммитом |
| 3 | `ReplacingMergeTree` по `event_id` в `ORDER BY` | Всё остальное: пачка пересобралась после ребаланса, и хеш блока другой |

### Правило для аналитика

**Третий уровень отложенный** — схлопывание происходит при фоновом слиянии,
срок которого не гарантирован. Поэтому сразу после дублирующей вставки
`SELECT count()` может показать 2.

Корректные способы посчитать:

```sql
SELECT uniq(event_id) FROM ugc.raw_events WHERE ...;   -- рекомендуемый
SELECT count() FROM ugc.raw_events FINAL WHERE ...;    -- точный, но дороже
SELECT argMax(payload, ingested_at) FROM ugc.raw_events GROUP BY event_id;
```

`SELECT count()` без `FINAL` **не является** корректным ответом на «сколько
было событий». Все три витрины построены на `uniq*` именно поэтому.

Отдельно: материализованное представление срабатывает на **вставляемый блок**, а
не на слитые данные, поэтому дубликат посчитался бы в витрине дважды. Отсюда
`deduplicate_blocks_in_dependent_materialized_views = 1` во всех вставках ETL.
Единственное `sum`-поле витрины (`watched_ms`) опирается на уровни 1–2 и в
аварийных сценариях считается приблизительным.

## Изменение схемы

DDL лежит в `etl_clickhouse/ddl/*.sql` и применяется одноразовым сервисом
`clickhouse-init` при каждом подъёме стека. Правила:

1. Каждый оператор — с `IF NOT EXISTS`: скрипт обязан быть идемпотентным.
2. Каждая таблица — `ON CLUSTER '{cluster}'`. Макрос делает файл переносимым
   между боевым кластером (4 узла) и тестовым (1 узел); второй набор DDL «для
   тестов» неизбежно разъехался бы с боевым.
3. `Replicated*MergeTree` **без аргументов пути** — путь берётся из
   `default_replica_path`, иначе переносимость ломается.
4. Добавили колонку в `raw_events` — не забудьте `RAW_COLUMNS` и
   `RAW_COLUMN_TYPES` в `src/transform/raw.py`: порядок должен совпадать.
