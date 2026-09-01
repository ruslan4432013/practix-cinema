# Архитектура: сервисы, хранилища, инфраструктура

Общая схема потоков — в [корневом README](../README.md#архитектура). Здесь —
состав стенда: что за чем стоит, на каких портах и почему именно так.
Устройство монорепозитория и принятые решения по структуре — в
[`monorepo.md`](monorepo.md).

## Сервисы и порты

| Сервис | Путь | Дистрибутив | Роль | Порт | `OTEL_SERVICE_NAME` |
|--------|------|-------------|------|------|---------------------|
| **Movies API** (`api`) | `apps/movies-api/` | `practix-movies-api` | Read-only films/genres/persons API (8 uvicorn workers) | expose 8000 | `movies-api` |
| **Auth** (`auth`) | `apps/auth/` | `practix-auth` | Пользователи, роли, JWT-сессии, OAuth | expose 8000 | `auth-service` |
| **Django Admin** (`django-admin`) | `apps/django-admin/` | вне workspace (Python 3.12) | Админка + внутренний content API | expose 8000 | `django-admin` |
| **Analytics Collector** | `apps/analytics-collector/` | `practix-analytics-collector` | Сбор пользовательских действий → Kafka (4 uvicorn workers) | expose 8000 | `analytics-collector` |
| **ETL ClickHouse** | `apps/etl-clickhouse/` | `practix-etl-clickhouse` | Конвейер Kafka → ClickHouse | host `9101` → 8000 (`/metrics`) | `etl-clickhouse` |
| **ETL Elasticsearch** (`etl`) | `apps/etl-elasticsearch/` | `practix-etl-elasticsearch` | Конвейер PostgreSQL → Elasticsearch (не путать с предыдущим) | — | — |
| **UGC API** (`ugc-api`) | `apps/ugc-api/` | `practix-ugc-api` | Оценки, закладки, рецензии и голоса (4 uvicorn workers) | expose 8000 | `ugc-api` |
| **Нотификации** | `apps/notifications/` | `practix-notifications` | Панель рассылок менеджера + сборщик писем (ходит в Auth за именем и адресом) + отправитель + планировщик (Django, gunicorn; профиль `notifications`) | `${NOTIFICATIONS_HOST_PORT:-8090}` | `notifications` / `notifications-builder` / `notifications-worker` / `notifications-scheduler` |
| **Websocket-шлюз** | `apps/notifications-ws/` | `practix-notifications-ws` | Мгновенная доставка уведомлений в открытую вкладку с деградацией на long polling (FastAPI, uvicorn; профиль `notifications`) | `${NOTIFICATIONS_WS_HOST_PORT:-8091}` | `notifications-ws` |
| **Сокращение ссылок** (`link-shortener`) | `apps/link-shortener/` | `practix-link-shortener` | Короткие ссылки для писем и подтверждение email по ним (4 uvicorn workers). В ЯДРЕ, не в профиле: nginx маршрутизирует `/s/` и без контейнера не стартует | expose 8000 | `link-shortener` |
| **Recommendations API** (`recommendations-api`) | `apps/recommendations-api/` | `practix-recommendations-api` | Похожие фильмы, персональные подборки, популярное (4 uvicorn workers). **Только читает витрину, ничего не считает.** В ЯДРЕ, не в профиле, по той же причине, что `ugc-api` и `link-shortener`: nginx маршрутизирует `/api/v1/recommendations` | expose 8000 | `recommendations-api` |
| **Recsys Trainer** | `apps/recsys-trainer/` | `practix-recsys-trainer` | Офлайн-обучение: датасет, со-встречаемость, ALS, раскладка витрины. Долгоживущий процесс с тиком раз в сутки (профиль `warehouse`, ровно один писатель — репликами не масштабируется) | host `9102` → 8000 (`/metrics`) | `recsys-trainer` |
| **Nginx** (`nginx`) | `infra/nginx/` | `nginx:1.25.3` | Reverse proxy / балансировщик, точка входа | published **80** | — |

Все одиннадцать Python-сервисов собираются ОДНИМ `infra/docker/python-service.Dockerfile`
(одиннадцать `--target`). У админки Django свой Dockerfile: она на Python 3.12, вне
общего uv workspace и со своим `uv.lock`.

`recsys-trainer` — **единственный таргет с системной зависимостью** (`libgomp1`):
`implicit` собран с OpenMP. Без неё образ собирается, а ALS падает в рантайме
внутри `except`, который честно публикует витрину без персональной выдачи.

Общий код — библиотеки в `libs/`: `practix-core` (логирование, request-id,
трассировка, backoff, обвязка JWT, определение IP клиента, пагинация),
`practix-contracts` (словарь событий и топиков, версионируется каталогами),
`practix-search-schema` (схемы индексов Elasticsearch), `practix-testing`
(фикстуры и скрипты ожидания сервисов).

## Хранилища и инфраструктура

| Компонент | Образ | Порт | Назначение |
|-----------|-------|------|-----------|
| **theatre-db** | `postgres:16` | `5432` | Фильмы (сид из `database_dump.sql`) |
| **auth-db** | `postgres:16` | host `5433` → `5432` | БД сервиса авторизации |
| **ugc-db** | `postgres:16` | host `5435` → `5432` | Оценки, закладки, рецензии и голоса. Отдельный экземпляр, чтобы нагрузка UGC не влияла на каталог и на вход в систему |
| **notifications-db** | `postgres:16` | host `5436` → `5432` | Кампании, шаблоны, журнал доставки (профиль `notifications`) |
| **shortener-db** | `postgres:16` | host `5437` → `5432` | Короткие ссылки. Отдельный экземпляр, как у UGC и нотификаций: сервис — свой владелец данных |
| **recs-db** | `postgres:16` | host `5438` → `5432` | Витрина рекомендаций: версии, похожие, персональные, популярное, снимок каталога. Источник истины — переживает перезапуск, в отличие от горячего слоя. Схемой владеет ВЫДАЧА, хотя пишет в неё батч: контейнер миграций переиспользует её образ |
| **elasticsearch** | `9.4.1` | `9200` | Поисковый индекс Movies API |
| **redis** | `redis:7-alpine` | `6379` | Кэш Movies API + denylist/сессии/rate-limit Auth. `maxmemory` + `volatile-lru`: под давлением вытесняются ключи кэша (у них есть TTL) |
| **redis-ugc** | `redis:7-alpine` | — (только внутри сети) | Аналитика: буфер деградации, rate limit и дедупликация коллектора. Отдельный экземпляр, чтобы многочасовой отказ Kafka не вытеснял сессии Auth. Политика `noeviction` — потерять ещё не доставленные события молча хуже, чем отбросить их с метрикой |
| **redis-recs** | `redis:7-alpine` | — (только внутри сети) | Горячий слой витрины рекомендаций: номер версии и списки с TTL. Политика `volatile-lru` — **не** `noeviction`, как у `redis-ugc`: все ключи имеют TTL и восстановимы из `recs-db`, поэтому вытеснение означает «медленнее», а не «потеряли». Отдельный экземпляр, чтобы ночная раскладка витрины не вытесняла сессии Auth и кэш каталога из общего `redis` |
| **kafka-0/1/2** | `apache/kafka:3.9.1` | host `9094`/`9095`/`9096` → `9094` | Кластер KRaft из трёх узлов (broker + controller), RF=3, `min.insync.replicas=2` |
| **kafka-ui** | `kafbat/kafka-ui` | `8081` → `8080` | Просмотр топиков, партиций и сообщений |
| **jaeger** | `jaegertracing/all-in-one:1.62.0` | UI `16686`, OTLP gRPC `4317`, OTLP HTTP `4318` | Сбор и просмотр трассировок |
| **clickhouse-01…04** | `clickhouse/clickhouse-server:25.3` | host `8123`/`8124`/`8125`/`8126` → `8123` | Аналитическое хранилище: 2 шарда × 2 реплики |
| **clickhouse-keeper-01…03** | `clickhouse/clickhouse-keeper:25.3` | `9181` | Кворум координации репликации и `ON CLUSTER`-DDL |
| **prometheus** | `prom/prometheus:v3.1.0` | `9090` | Метрики ETL, коллектора, выдачи и обучения рекомендаций и всех узлов ClickHouse + алерты (в том числе свежесть витрины: `RecsShelfStale`, `RecsShelfMissing`, `RecsCatalogCoverageLow`) |
| **grafana** | `grafana/grafana:11.5.0` | `3000` | Дашборд «UGC ETL → ClickHouse» (память, лаг, вставки) |
| **glitchtip** | `glitchtip/glitchtip:6` | `8001` → `8000` | Sentry-совместимый приёмник ошибок всех двенадцати сервисов (`SERVER_ROLE=all_in_one`) |
| **glitchtip-db** | `postgres:16` | — (только внутри сети) | БД приёмника ошибок |
| **glitchtip-valkey** | `valkey/valkey:9-alpine` | — (только внутри сети) | Очередь задач приёмника. Отдельный экземпляр, чтобы не вытеснять сессии Auth из общего `redis` |
| **elasticsearch-logs** | `9.4.1` | `9210` → `9200` | Хранилище логов, индексы `practix-logs-YYYY.MM.DD`. Отдельный узел от поискового: лог растёт непрерывно, и упёршись в диск, он унёс бы с собой поиск фильмов |
| **logstash** | `9.4.1` | `5044` (beats), `9600` (API) | Разбор JSON, прикладное время в `@timestamp`, подпись сервисом, приведение типов nginx |
| **kibana** | `9.4.1` | `5601` | Просмотр логов, data view `practix-logs-*` |
| **filebeat** | `9.4.1` | — (только внутри сети) | Читает `/var/lib/docker/containers/*/*.log`, отсеивает чужие проекты и собственные контейнеры ELK |

**Вспомогательные one-shot сервисы:** `auth-migrations` (`alembic upgrade head`), `ugc-migrations`, `shortener-migrations` и `recs-migrations` (то же для своих баз; последний переиспользует образ `recommendations-api` — миграции витрины живут в её пакете, а не в пакете батча), `django-migrations` (`migrate` + `collectstatic` + `createsuperuser`), `kafka-init` (идемпотентное создание топиков UGC), `clickhouse-init` (идемпотентное применение DDL хранилища) и `elasticsearch-logs-init` (шаблон индекса логов — без него тип поля закрепляет тот, кто записал первым). Порядок запуска определяется healthcheck'ами и `depends_on`.

**Volumes:** `pg_data`, `redis_data`, `redis_ugc_data`, `esdata`, `etl_state`, `static_volume`, `auth_pg_data`, `ugc_pg_data`, `shortener_pg_data`, `recs_pg_data`, `redis_recs_data`, `recs_trainer_data`, `notifications_pg_data`, `rabbitmq_data`, `kafka_0_data`, `kafka_1_data`, `kafka_2_data`, `ch_01_data`…`ch_04_data`, `ch_keeper_01_data`…`ch_keeper_03_data`, `prometheus_data`, `grafana_data`, `glitchtip_pg_data`, `glitchtip_uploads`, `es_logs_data`, `logstash_data`, `kibana_data`, `filebeat_data`.

> **Ресурсы.** Полный стек — это 58 контейнеров (28 из них ядро), из них четыре
> узла ClickHouse. Docker нужно выделить **не менее 12 ГБ RAM**; ограничения
> памяти самих узлов заданы в `clickhouse/config/limits.xml`.

> **Две схемы именования настроек.** Movies API использует настройки в **нижнем регистре** (`settings.redis_host`), Auth — в **ВЕРХНЕМ** (`settings.REDIS_HOST`). Не копируйте имена полей между сервисами. Конфигурация всех сервисов (включая Django admin — через `example/config.py`) построена на **pydantic-settings**.
