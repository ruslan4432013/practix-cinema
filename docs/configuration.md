# Конфигурация (переменные окружения)

Корневой `.env` (шаблон — `.env.example`) драйвит все сервисы compose. Ключевые группы:

| Группа | Переменные |
|--------|-----------|
| **Rate limit** (Auth) | `RATE_LIMIT_ENABLED`, `RATE_LIMIT_TIMES` (20), `RATE_LIMIT_SECONDS` (60) |
| **Трассировка** | `OTEL_ENABLED`, `OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318` (per-service `OTEL_SERVICE_NAME` задаётся в compose) |
| **Сбор ошибок** | `SENTRY_ENABLED`, `SENTRY_DSN`, `SENTRY_ENVIRONMENT`, `SENTRY_RELEASE`, `SENTRY_SAMPLE_RATE`, `SENTRY_SEND_DEFAULT_PII`. Один DSN на все сервисы, различает их тег `service` (берётся из `OTEL_SERVICE_NAME`) |
| **GlitchTip** | `GLITCHTIP_SECRET_KEY` (**обязательно заменить**), `GLITCHTIP_DOMAIN`, `GLITCHTIP_HOST_PORT`, `GLITCHTIP_ENABLE_OPEN_USER_REGISTRATION`, `GLITCHTIP_POSTGRES_*`, `GLITCHTIP_EMAIL_URL` |
| **Логи** | `LOG_LEVEL`, `LOG_JSON` (все сервисы). ELK: `ELASTIC_LOGS_HOST_PORT` (9210), `KIBANA_HOST_PORT` (5601), `LOGSTASH_BEATS_HOST_PORT` (5044), `ES_LOGS_JAVA_OPTS`, `LS_JAVA_OPTS`, `KIBANA_ENCRYPTION_KEY`, `COMPOSE_PROJECT_NAME` (по нему Filebeat отсеивает контейнеры чужих проектов) |
| **Интеграция Auth** (Movies API / Django) | `AUTHJWT_SECRET_KEY`, `AUTH_API_URL=http://auth:8000`, `AUTH_REQUEST_TIMEOUT` / `AUTH_API_TIMEOUT=2.0`, `AUTH_API_MAX_ATTEMPTS=3` |
| **OAuth** | `OAUTH_ENABLED=False`, `YANDEX_CLIENT_ID`, `YANDEX_CLIENT_SECRET`, `YANDEX_REDIRECT_URI=http://localhost/api/v1/oauth/yandex/callback` |
| **PostgreSQL (фильмы)** | `POSTGRES_HOST` / `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` |
| **PostgreSQL (Auth)** | `AUTH_POSTGRES_HOST` / `AUTH_POSTGRES_PORT` / `AUTH_POSTGRES_DB` / `AUTH_POSTGRES_USER` / `AUTH_POSTGRES_PASSWORD` |
| **Elasticsearch / Redis** | `ELASTIC_HOST` / `ELASTIC_PORT`, `REDIS_HOST` / `REDIS_PORT` |
| **Django superuser** | `DJANGO_SUPERUSER_USERNAME` / `DJANGO_SUPERUSER_PASSWORD` / `DJANGO_SUPERUSER_EMAIL` |
| **Kafka** | `KAFKA_CLUSTER_ID` (одинаковый у всех узлов), `KAFKA_BOOTSTRAP_SERVERS`, `KAFKA_TOPIC_*` |
| **Analytics Collector** | `UGC_ENV` (`prod` включает обязательные проверки безопасности), `UGC_REDIS_DB=1`, `UGC_FALLBACK_*` (буфер деградации), `UGC_MAX_BODY_BYTES` / `UGC_MAX_BATCH_SIZE`, `UGC_RATE_LIMIT_*` (600/60 с), `UGC_IP_HASH_SALT` (**обязательно заменить в проде**), `UGC_CORS_ALLOW_ORIGINS` |
| **ClickHouse** | `CH_CLUSTER=ugc_cluster`, `CH_DATABASE=ugc`, `CH_USER` / `CH_PASSWORD` (**обязательно заменить в проде**), `CH_HOSTS` (по одному узлу на шард — это координаторы вставки, а не реплики), `CH_INSERT_QUORUM=2` (аналог acks=all + ISR=2: отказ реплики становится лагом, а не потерей), `CH_RETRY_*`, `CH_SEND_RECEIVE_TIMEOUT` |
| **ETL ClickHouse** | `ETL_CONSUMER_GROUP`, `ETL_FLUSH_INTERVAL` / `ETL_BATCH_MAX_ROWS` / `ETL_BATCH_MAX_BYTES` (три порога закрытия пачки), `ETL_FETCH_MAX_BYTES` / `ETL_MAX_PARTITION_FETCH_BYTES` (**реальные рычаги потребления памяти**), `ETL_MAX_SCHEMA_VERSION`, `ETL_METRICS_PORT`, `ETL_TRACEMALLOC_*`, `ETL_RSS_WARN_MB` |
| **Grafana** | `GF_SECURITY_ADMIN_PASSWORD` |

Все публикуемые порты — тоже переменные (`NGINX_HOST_PORT`, `JAEGER_UI_HOST_PORT`
и прочие), поэтому адреса из [быстрого старта](quickstart.md) меняются вместе с
ними. Состав хранилищ и портов по умолчанию — в [`architecture.md`](architecture.md).
