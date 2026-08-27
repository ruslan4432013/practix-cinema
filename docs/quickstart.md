# Быстрый старт и эксплуатация стенда

Compose-файлы лежат в `infra/compose/`, а не в корне, поэтому команда всегда
идёт с тремя ключами: `--env-file` (корневой `.env` — единственный источник
переменных), `-f` (какой файл) и `--project-directory` (относительно чего
разрешаются пути монтирования). Ниже это сокращено до `$DC`.

```bash
# 0. Подготовить .env и завести короткий алиас команды
cp .env.example .env
DC="docker compose --env-file .env -f infra/compose/docker-compose.yml --project-directory infra/compose"

# 1. Поднять стек из корня репозитория. Ядро — 12 сервисов; профили добавляются
#    по одному, всё вместе просит у Docker ≥16 ГБ (см. docs/architecture.md).
$DC up -d --build
#   + --profile warehouse       ClickHouse, Keeper, ETL ClickHouse
#   + --profile observability   Prometheus, Grafana, Kafka UI, GlitchTip
#   + --profile logging         Elasticsearch (логи), Logstash, Kibana, Filebeat
#   + --profile notifications   Панель рассылок, воркер, планировщик, RabbitMQ, Mailpit
#                               ПРОФИЛЬ ОБЯЗАН ПОВТОРЯТЬСЯ И НА `down -v` — иначе
#                               контейнеры и тома переживают teardown.

# 2. Заполнить theatre-db тестовыми данными (110 жанров / 5 000 персон / 200 000 фильмов)
uv run --with psycopg2-binary --with faker python seed_db.py

# 3. Создать суперпользователя в сервисе Auth (идемпотентно; создаёт роль 'admin', если её нет).
#    Подкоманда пишется явно: команд в CLI две, схлопывание Typer снято callback'ом.
$DC exec auth python -m practix_auth.cli createsuperuser \
    --login admin --email admin@example.com --password 'Admin12345!'

# 3a. Служебная учётка сервиса сокращения ссылок — узкая роль вместо admin.
$DC exec auth python -m practix_auth.cli create-service-account \
    --login svc-link-shortener --email svc-link-shortener@example.com \
    --password 'Svc-Short123!' --role email-confirmer

# 4. Проверить сквозной путь события: отправить клик и найти его в ClickHouse
curl -X POST http://localhost/api/v1/events/click -H 'Content-Type: application/json' \
     -d '{"session_id":"demo","element_type":"film","page_url":"http://localhost/"}'
sleep 5   # ETL закрывает пачку по таймеру ETL_FLUSH_INTERVAL
$DC exec clickhouse-01 clickhouse-client --user etl --password etl \
  -q "SELECT event_type, count() FROM ugc.raw_events GROUP BY event_type"
# промежуточные точки: http://localhost:8081 (Kafka UI), http://localhost:9101/metrics (ETL)

# 5. Метки просмотра и витрины: имитируем зрителя, бросившего фильм на середине
FILM=$(uuidgen | tr 'A-Z' 'a-z')
for POS in 0 12000 24000 36000 48000 60000; do
  curl -s -X POST http://localhost/api/v1/events/custom -H 'Content-Type: application/json' \
    -d "{\"event_type\":\"video_progress\",\"session_id\":\"demo\",\"film_id\":\"$FILM\",\
\"playback_position_ms\":$POS,\"duration_ms\":120000}" > /dev/null
done
sleep 5
$DC exec clickhouse-01 clickhouse-client --user etl --password etl \
  -q "SELECT progress_pct, uniqMerge(views) FROM ugc.film_retention_daily \
      WHERE film_id='$FILM' GROUP BY progress_pct ORDER BY progress_pct"
# ожидаем бакеты 0,10,20,30,40,50 — ровно до той точки, где зритель ушёл

# Миграции Auth и UGC (Alembic) отдельной командой НЕ нужны: их прогоняют
# одноразовые сервисы auth-migrations и ugc-migrations, и сами сервисы ждут их
# завершения. Вручную — целями Nx:
npx nx run auth:migrate
npx nx run ugc-api:migrate
```

Адреса ниже записаны для значений портов по умолчанию из `.env.example`. Все
публикуемые порты — переменные (`NGINX_HOST_PORT`, `JAEGER_UI_HOST_PORT` и
прочие), и если в вашем `.env` они переопределены, адрес меняется вместе с ними.

ETL автоматически перенесёт данные из PostgreSQL в Elasticsearch, а
`etl-clickhouse` — поток пользовательских действий из Kafka в ClickHouse.

**Полезные адреса:**

- Swagger Movies API — http://localhost/api/openapi
- Swagger Auth — `/api/openapi` контейнера auth
- Jaeger UI — http://localhost:16686
- Kafka UI — http://localhost:8081
- Prometheus — http://localhost:9090 (вкладка Targets: все цели должны быть UP)
- Grafana — http://localhost:3000, дашборд «UGC ETL → ClickHouse» (`admin` / `$GF_SECURITY_ADMIN_PASSWORD`)
- ClickHouse HTTP — http://localhost:8123 (узлы 2–4 на портах 8124–8126)
- Kibana — http://localhost:5601 (профиль `logging`), Elasticsearch логов — http://localhost:9210
- Панель рассылок — http://localhost:8090/admin/ (профиль `notifications`, вход `admin`/`admin`),
  RabbitMQ — http://localhost:15672, принятые письма — http://localhost:8025
- Витрина деградации websocket → long polling → лента — http://localhost:8090/demo/cabinet
  (профиль `notifications`; websocket-шлюз слушает http://localhost:8091)

## Проверка отказоустойчивости ETL

```bash
# Отказ ХРАНИЛИЩА: данные не теряются, а ждут в Kafka
docker compose exec clickhouse-01 clickhouse-client --user etl --password etl \
  -q "SELECT count() FROM ugc.raw_events"        # запомнить N
docker compose stop clickhouse-01 clickhouse-02 clickhouse-03 clickhouse-04
for i in $(seq 1 100); do curl -s -X POST http://localhost/api/v1/events/click \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"outage","element_type":"film"}' > /dev/null; done

curl -s http://localhost:9101/metrics | grep -E 'etl_ch_clickhouse_up|etl_ch_paused_partitions'
# ожидаем 0 и >0: включился backpressure

docker compose exec kafka-0 /opt/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server kafka-0:9092 --describe --group ugc-clickhouse-etl
# LAG растёт, а CURRENT-OFFSET стоит — оффсеты не прокоммичены, значит потери нет

docker compose start clickhouse-01 clickhouse-02 clickhouse-03 clickhouse-04
# через ~минуту: LAG=0, count() = N + 100

# Отказ ИСТОЧНИКА: ETL не падает и не теряет позицию
docker compose stop kafka-0 kafka-1 kafka-2     # WARNING в логах, процесс жив
docker compose start kafka-0 kafka-1 kafka-2    # чтение продолжается с последнего коммита

# Отравленное сообщение не блокирует поток
docker compose exec kafka-0 bash -c "echo '{not a json' | \
  /opt/kafka/bin/kafka-console-producer.sh --bootstrap-server kafka-0:9092 --topic ugc.clicks.v1"
docker compose exec clickhouse-01 clickhouse-client --user etl --password etl \
  -q "SELECT error_kind, error_text FROM ugc.invalid_events ORDER BY ingested_at DESC LIMIT 5"
# после этого шаг 4 повторить — событие всё равно доезжает
```

## Проверка на утечку памяти

Оставить нагрузку на `/api/v1/events/batch` на 30–60 минут и смотреть в Grafana
панель «RSS процесса»: кривая обязана выйти на плато. Одновременно
`etl_ch_allocated_blocks` должен колебаться вокруг постоянного значения, а не
расти монотонно. Если растёт — перезапустить ETL с
`ETL_TRACEMALLOC_ENABLED=True` и читать INFO-логи с топ-10 приростов аллокаций.
