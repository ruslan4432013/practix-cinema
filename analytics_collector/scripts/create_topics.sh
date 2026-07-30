#!/bin/bash
# Создание топиков сервиса сбора пользовательских действий.
#
# Скрипт запускается одноразовой задачей kafka-init при каждом подъёме стека
# (по образцу auth-migrations), поэтому обязан быть идемпотентным:
# --if-not-exists не даёт упасть на уже существующем топике.
#
# Топики заводятся явно, а не автосозданием: автосозданный топик получает
# настройки по умолчанию (одна партиция, RF=1), то есть молча теряет и
# параллелизм, и отказоустойчивость.
#
# Обоснование числа партиций и сроков хранения — analytics_collector/docs/kafka_topics.md.
set -euo pipefail

BOOTSTRAP="${KAFKA_BOOTSTRAP:-kafka-0:9092,kafka-1:9092,kafka-2:9092}"
KAFKA_BIN=/opt/kafka/bin

# Три реплики каждой партиции при подтверждении записи двумя (acks=all +
# min.insync.replicas=2): кластер переживает отказ одного брокера без остановки
# записи и без потери подтверждённых данных.
REPLICATION_FACTOR="${REPLICATION_FACTOR:-3}"
MIN_INSYNC_REPLICAS="${MIN_INSYNC_REPLICAS:-2}"

RETENTION_7D=$((7 * 24 * 60 * 60 * 1000))
RETENTION_30D=$((30 * 24 * 60 * 60 * 1000))

create_topic() {
  local name="$1" partitions="$2" retention_ms="$3"

  echo "==> topic ${name} (partitions=${partitions}, rf=${REPLICATION_FACTOR}, retention=${retention_ms}ms)"
  "${KAFKA_BIN}/kafka-topics.sh" \
    --bootstrap-server "${BOOTSTRAP}" \
    --create --if-not-exists \
    --topic "${name}" \
    --partitions "${partitions}" \
    --replication-factor "${REPLICATION_FACTOR}" \
    --config "min.insync.replicas=${MIN_INSYNC_REPLICAS}" \
    --config "retention.ms=${retention_ms}" \
    --config "cleanup.policy=delete" \
    --config "compression.type=producer" \
    --config "max.message.bytes=1048576"

  # Топик мог существовать с прежними настройками (например, после смены
  # retention в конфигурации) — --if-not-exists их не обновит, поэтому
  # применяем конфигурацию отдельной командой.
  "${KAFKA_BIN}/kafka-configs.sh" \
    --bootstrap-server "${BOOTSTRAP}" \
    --alter --entity-type topics --entity-name "${name}" \
    --add-config "min.insync.replicas=${MIN_INSYNC_REPLICAS},retention.ms=${retention_ms}" >/dev/null
}

echo "Waiting for Kafka cluster at ${BOOTSTRAP}..."
for _ in $(seq 1 30); do
  if "${KAFKA_BIN}/kafka-broker-api-versions.sh" --bootstrap-server "${BOOTSTRAP}" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

# Клики и просмотры страниц — самые массовые потоки, им больше партиций.
# Хранение 7 дней: это «сырой» поток для оперативной аналитики, долговременное
# хранение — задача витрины в колоночной СУБД, а не Kafka.
create_topic "${KAFKA_TOPIC_CLICKS:-ugc.clicks.v1}"              6 "${RETENTION_7D}"
create_topic "${KAFKA_TOPIC_PAGE_VIEWS:-ugc.page_views.v1}"      6 "${RETENTION_7D}"

# События плеера и поиска на порядок реже, но ценнее для продуктовых метрик,
# поэтому меньше партиций и дольше хранение.
create_topic "${KAFKA_TOPIC_VIDEO_EVENTS:-ugc.video_events.v1}"  3 "${RETENTION_30D}"
create_topic "${KAFKA_TOPIC_SEARCH_EVENTS:-ugc.search_events.v1}" 3 "${RETENTION_30D}"

# Метки прогресса просмотра — самый массовый поток в системе: тик раз в 30 с на
# каждого зрителя, то есть ~240 событий на двухчасовой фильм. 12 партиций,
# вдвое больше, чем у кликов: именно этот топик первым упрётся в потолок
# параллелизма чтения ETL, а число партиций задаёт верхнюю границу числа
# одновременно работающих консьюмеров группы.
# Хранение 7 дней: сырые тики нужны только до того, как их свернут витрины
# ClickHouse, долговременное хранение — задача хранилища, а не Kafka.
create_topic "${KAFKA_TOPIC_VIDEO_PROGRESS:-ugc.video_progress.v1}" 12 "${RETENTION_7D}"

# DLQ: сюда попадает то, что не удалось доставить после исчерпания попыток.
# Одна партиция — поток должен быть пустым, а порядок разбора важнее скорости.
create_topic "${KAFKA_TOPIC_DLQ:-ugc.events.dlq.v1}"             1 "${RETENTION_30D}"

echo
echo "==> Итоговое состояние топиков:"
"${KAFKA_BIN}/kafka-topics.sh" --bootstrap-server "${BOOTSTRAP}" --describe | grep -E '^Topic: ugc\.' || true
echo "Topics are ready."
