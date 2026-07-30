#!/bin/bash
# Шаблон индекса для practix-logs-*. Идемпотентен: PUT перезаписывает шаблон
# целиком, повторный запуск ничего не ломает.
#
# ПОЧЕМУ ЭТО НЕ «ОПЦИОНАЛЬНАЯ ОПТИМИЗАЦИЯ». Без шаблона тип поля закрепляет
# ТОТ, КТО ЗАПИСАЛ ПЕРВЫМ. На живом стенде первым про `status` написал Jaeger —
# строкой «IDLE», — и `status` в индексе стал text: диапазонный запрос
# `status >= 500` по логам nginx перестал работать молча, вернув 4xx-строки
# лексикографически. Шаблон применяется ДО первой записи, поэтому Logstash
# ждёт завершения этого контейнера.
#
# ignore_malformed на уровне индекса — вторая половина ответа. Поля из
# extra=... в коде попадают в корень документа, и первый же сервис, залогировавший
# `status` строкой, получил бы отказ ВСЕГО документа (событие ушло бы в DLQ, а
# в Kibana выглядело бы как «сервис перестал логировать»). С этим флагом
# Elasticsearch пропускает только неподходящее ПОЛЕ, оставляя документ; исходное
# значение по-прежнему видно в _source.
set -euo pipefail

ES_URL="${ES_LOGS_URL:-http://elasticsearch-logs:9200}"

echo "Ожидаем Elasticsearch логов: ${ES_URL}"
for _ in $(seq 1 60); do
    if curl -fsS "${ES_URL}/_cluster/health" >/dev/null 2>&1; then
        break
    fi
    sleep 2
done

echo "Применяем шаблон индекса practix-logs"
curl -fsS -X PUT "${ES_URL}/_index_template/practix-logs" \
    -H 'Content-Type: application/json' \
    -d '{
  "index_patterns": ["practix-logs-*"],
  "priority": 100,
  "template": {
    "settings": {
      "number_of_shards": 1,
      "number_of_replicas": 0,
      "index.mapping.ignore_malformed": true,
      "index.mapping.total_fields.limit": 2000
    },
    "mappings": {
      "properties": {
        "@timestamp":             {"type": "date"},
        "shipped_at":             {"type": "date"},
        "service":                {"type": "keyword"},
        "level":                  {"type": "keyword"},
        "logger":                 {"type": "keyword"},
        "request_id":             {"type": "keyword"},
        "stream":                 {"type": "keyword"},
        "message":                {"type": "text"},
        "exception":              {"type": "text"},
        "status":                 {"type": "integer"},
        "body_bytes_sent":        {"type": "long"},
        "request_length":         {"type": "long"},
        "request_time":           {"type": "float"},
        "method":                 {"type": "keyword"},
        "uri":                    {"type": "keyword"},
        "remote_addr":            {"type": "ip"},
        "upstream_addr":          {"type": "keyword"},
        "upstream_status":        {"type": "keyword"},
        "upstream_response_time": {"type": "keyword"},
        "upstream_connect_time":  {"type": "keyword"}
      }
    }
  }
}'
echo
echo "Шаблон practix-logs применён"
