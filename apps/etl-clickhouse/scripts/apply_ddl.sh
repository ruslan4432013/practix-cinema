#!/bin/bash
# Применение DDL аналитического хранилища.
#
# Запускается одноразовой задачей clickhouse-init при каждом подъёме стека
# (по образцу kafka-init и auth-migrations), поэтому обязан быть идемпотентным:
# каждый оператор в ddl/*.sql содержит IF NOT EXISTS.
#
# DDL живёт в etl_clickhouse/, а не в clickhouse/, по той же логике, по которой
# create_topics.sh лежит в analytics_collector/: схемой владеет и версионирует
# приложение, а инфраструктурный контейнер только применяет её.
set -euo pipefail

HOST="${CH_INIT_HOST:-clickhouse-01}"
CLUSTER="${CH_CLUSTER:-ugc_cluster}"
DATABASE="${CH_DATABASE:-ugc}"
EXPECTED_NODES="${CH_EXPECTED_NODES:-4}"
USER="${CH_USER:-etl}"
PASSWORD="${CH_PASSWORD:-etl}"

MAX_ATTEMPTS=60
DELAY_SECONDS=2

# Подключаемся под учёткой ETL, а не под default: официальный образ отключает
# сетевой доступ для default, когда ему не задан пароль, поэтому из соседнего
# контейнера default не работает вовсе. Заодно схема создаётся тем же
# пользователем, который потом в неё пишет.
ch() { clickhouse-client --host "${HOST}" --user "${USER}" --password "${PASSWORD}" "$@"; }

wait_for() {
  local description="$1"
  shift
  echo "Waiting for ${description}..."
  for _ in $(seq 1 "${MAX_ATTEMPTS}"); do
    if "$@" >/dev/null 2>&1; then
      echo "  ok: ${description}"
      return 0
    fi
    sleep "${DELAY_SECONDS}"
  done
  echo "  FAILED: ${description} did not become ready in time" >&2
  # Печатаем последнюю ошибку целиком: молчаливый таймаут в одноразовой задаче
  # разбирать невозможно — контейнер уже мёртв, а логов нет.
  "$@" || true
  return 1
}

cluster_is_assembled() {
  local n
  n=$(ch --query "SELECT count() FROM system.clusters WHERE cluster='${CLUSTER}'" 2>/dev/null || echo 0)
  [ "${n}" -ge "${EXPECTED_NODES}" ]
}

wait_for "ClickHouse at ${HOST}" ch --query 'SELECT 1'

# Одного «сервер отвечает» мало. ON CLUSTER-DDL кладёт задачи в очередь в
# Keeper и ждёт исполнения на всех узлах: если хоть один ещё не
# зарегистрировался в кластере или Keeper недоступен, половина операторов
# упадёт по таймауту DDL-очереди, и схема окажется применена частично.
wait_for "all ${EXPECTED_NODES} nodes of cluster '${CLUSTER}'" cluster_is_assembled
wait_for "Keeper quorum" ch --query "SELECT * FROM system.zookeeper WHERE path='/' FORMAT Null"

for f in /ddl/*.sql; do
  echo "==> ${f}"
  ch --queries-file "${f}" \
     --distributed_ddl_task_timeout=300 \
     --distributed_ddl_output_mode=throw
done

echo
echo "==> Итоговое состояние схемы:"
ch --query "SELECT name, engine FROM system.tables WHERE database='${DATABASE}' ORDER BY name FORMAT PrettyCompact"
echo "Schema is ready."
