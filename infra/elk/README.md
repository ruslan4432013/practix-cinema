# Сбор логов: Filebeat → Logstash → Elasticsearch → Kibana

Профиль compose — `logging`. Все четыре сервиса выключены по умолчанию.

```bash
docker compose --env-file .env -f infra/compose/docker-compose.yml \
               --project-directory infra/compose --profile logging up -d
```

Kibana — `http://localhost:5601`, Elasticsearch логов — `http://localhost:9210`
(порты параметризованы: `KIBANA_HOST_PORT`, `ELASTIC_LOGS_HOST_PORT`).

## Что происходит с одной строкой лога

1. Приложение печатает JSON в **stdout** и о существовании ELK ничего не знает.
2. Docker штатным драйвером `json-file` кладёт строку в
   `/var/lib/docker/containers/<id>/<id>-json.log`.
3. **Filebeat** читает этот файл, склеивает разрезанные docker'ом куски,
   добавляет имя контейнера и отбрасывает чужое (см. ниже).
4. **Logstash** разбирает JSON, ставит прикладное время в `@timestamp`,
   подписывает запись сервисом, приводит типы полей nginx.
5. **Elasticsearch** хранит документ в индексе `practix-logs-YYYY.MM.DD`.
6. **Kibana** показывает его в Discover по data view `practix-logs-*`.

## Контракт полей

Одинаков у всех сервисов — на нём держатся и разбор в Logstash, и запросы в
Kibana.

| Поле | Откуда | Заметки |
|---|---|---|
| `timestamp` | `practix_core.logging.JsonFormatter`, `$time_iso8601` у nginx | после разбора переезжает в `@timestamp` и из документа удаляется |
| `@timestamp` | Logstash | прикладное время события |
| `shipped_at` | Logstash | момент захвата строки docker'ом; разница с `@timestamp` — лаг доставки |
| `level` | форматтер / `"INFO"` у nginx | `stderr` без нашего формата повышается до `WARNING` |
| `logger` | имя Python-логгера, `nginx.access` у nginx | |
| `message` | текст записи, `$request` у nginx | |
| `request_id` | `RequestIdFilter` (contextvar), `$request_id` у nginx | сквозной идентификатор — **главное поле при разборе инцидента** |
| `service` | `static_fields={'service': OTEL_SERVICE_NAME}` | у чужих образов подставляется имя контейнера |
| `exception` | форматтер | текст traceback'а |
| прочее | `extra={...}` в коде | попадает в корень документа как есть |

Nginx добавляет `status`, `request_time`, `upstream_*`, `method`, `uri` и
остальные поля access-лога — см. `infra/nginx/nginx.conf`.

**Строки чужих образов на поля НЕ раскладываются.** Logstash разбирает JSON
только у строк, начинающихся с `{"timestamp":`, — это наш формат, и порядок
ключей в нём закреплён тестом. У postgres, kafka, redis, Jaeger и Elasticsearch
строка целиком лежит в `message`, `service` берётся из имени контейнера, а
`stderr` без нашего формата помечается уровнем `WARNING`.

Причина ограничения проверена на стенде: половина инфраструктуры тоже пишет
JSON, но свой. У Elasticsearch и Jaeger `service` — **объект** (ECS:
`service.name`), у нас строка; у Jaeger `status` — строка `"IDLE"`, у nginx
число. Elasticsearch закрепляет тип за тем, кто записал первым, и отвергает
остальные документы целиком: разбор «всего, что похоже на JSON» за три минуты
положил в DLQ 500 КБ наших собственных событий.

**Один `request_id` находит и строку nginx, и строки приложения** — это и есть
смысл всей конструкции:

```
request_id: "3ec1f8b4..."
```

## Почему Filebeat, а не драйвер gelf

Драйвер `gelf` настраивается одной строкой в compose и был бы проще. Он шлёт
логи **по UDP без подтверждения**: перезапуск Logstash теряет записи молча, и
потерю нечем обнаружить — обе стороны отработали штатно. Вдобавок при
не-`json-file` драйвере перестаёт работать `docker compose logs`, которым стенд
пользуются каждый день.

Filebeat читает те же файлы, что и `docker logs`, ничего не ломает, помнит
смещения между перезапусками и ждёт, пока Logstash вернётся.

Прямая отправка из приложения (`python-logstash`) отвергнута по третьей причине:
она жёстко привязывает сервис к получателю логов, и смена системы логирования
становится правкой кода в каждом сервисе.

## Две ловушки, на которых это ломается

**Петля обратной связи.** Logstash пишет строку на каждый принятый батч. Эта
строка попадает в свой `json-file`, Filebeat её читает и отправляет в Logstash,
тот пишет следующую. Система входит в самоподдерживающийся режим и упирается в
диск. Разрывается фильтром `drop_event` по `container.name` в `filebeat.yml` —
**до** отправки, а не на стороне Logstash. Симптом, если фильтр перестал
работать: счётчик документов растёт при полном отсутствии трафика.

**Чужие контейнеры.** Filebeat видит весь docker-хост, а не только наш стенд: на
машине разработчика рядом крутятся контейнеры других проектов. Отсекаются по
метке `com.docker.compose.project` — отсюда переменная `COMPOSE_PROJECT_NAME` в
`.env` (по умолчанию `compose`, из `--project-directory infra/compose`). Если
стенд поднимают с `-p practix`, переменную надо поменять вместе с ним.

## Разбор проблем

```bash
dc() { docker compose --env-file .env -f infra/compose/docker-compose.yml \
                      --project-directory infra/compose "$@"; }

# индексы есть?
curl -s 'http://localhost:9210/_cat/indices?v' | grep practix-logs

# какие сервисы вообще пишут
curl -s -H 'Content-Type: application/json' \
  'http://localhost:9210/practix-logs-*/_search?size=0' \
  -d '{"aggs":{"svc":{"terms":{"field":"service.keyword","size":50}}}}'

# записи, которые не разобрались
curl -s 'http://localhost:9210/practix-logs-*/_count?q=tags:_jsonparsefailure'
curl -s 'http://localhost:9210/practix-logs-*/_count?q=tags:_dateparsefailure'

# записи, ОТВЕРГНУТЫЕ Elasticsearch — первое место, куда смотреть, если сервис
# «перестал логировать»
dc --profile logging exec logstash ls -R data/dead_letter_queue

# читает ли Filebeat файлы вообще
dc --profile logging logs filebeat | tail -30
```

**«Индекс пустой, ошибок нет»** — почти всегда права на файлы: каталоги
`/var/lib/docker/containers/<id>/` имеют режим `0700`, и без `user: root`
Filebeat стартует штатно и не находит ни одного файла.

**«Логи ушли в `.ds-logs-generic-default-*`»** — в выводе Logstash пропал
`data_stream => false`: плагин в режиме `auto` на ES 9.x игнорирует собственное
имя индекса.

**«Сервис перестал логировать»** — конфликт типов в маппинге. Поля из `extra=`
идут в корень документа, и сервис, залогировавший `status` строкой после того,
как nginx закрепил его целым числом, получил бы отказ **всего** документа.
`ignore_malformed` в шаблоне индекса это гасит (см. ниже), но если запись всё же
отвергнута — она в DLQ, и там же причина.

## Типы полей: почему шаблон индекса обязателен

Типы закрепляет `infra/elk/scripts/apply_index_template.sh`, который применяет
одноразовый контейнер `elasticsearch-logs-init` — по образцу `kafka-init` и
`clickhouse-init`. Logstash ждёт его завершения
(`condition: service_completed_successfully`), потому что шаблон обязан
существовать ДО первой записи.

Это не оптимизация, а исправление конкретной поломки, воспроизведённой на
стенде. Без шаблона тип поля закрепляет **тот, кто записал первым**: про
`status` первым написал Jaeger — строкой `"IDLE"`, — поле стало `text`, и
запрос `status >= 500` по логам nginx перестал работать **молча**, сравнивая
коды ответов лексикографически.

Вторая половина ответа — `index.mapping.ignore_malformed: true`. Поля из
`extra=...` попадают в корень документа, и первый сервис, залогировавший
`status` строкой, получил бы отказ **всего** документа (событие ушло бы в DLQ, а
в Kibana это выглядит как «сервис перестал логировать»). С флагом Elasticsearch
пропускает только неподходящее поле; значение остаётся видно в `_source`.

Шаблон заодно ставит `number_of_replicas: 0` — одиночный узел не может
разместить реплику, и без этого индекс вечно `YELLOW`.

Правка типов — правка скрипта плюс `docker compose ... --profile logging up -d
elasticsearch-logs-init` (PUT идемпотентен). Уже созданные индексы при этом не
меняются: маппинг существующего индекса неизменяем, нужен новый индекс (то есть
следующие сутки) или переиндексация.

## Ретеншен

**ILM не настроен**: `practix-logs-YYYY.MM.DD` растут бесконечно, а
`uvicorn.access` включён у movies-api, auth и ugc-api — на каждый запрос
приходится минимум два документа (nginx + uvicorn). Аварийные выходы:

```bash
curl -X DELETE 'http://localhost:9210/practix-logs-2026.07.*'   # удалить старое
# или LOG_LEVEL=WARNING в .env — уменьшить поток
```

Настроенный ILM — следующая задача, а не сделанная.

## Data view в Kibana

UI: **Management → Stack Management → Data Views → Create data view**, паттерн
`practix-logs-*`, поле времени `@timestamp`. То же самое запросом:

```bash
curl -s -X POST 'http://localhost:5601/api/data_views/data_view' \
  -H 'kbn-xsrf: true' -H 'Content-Type: application/json' \
  -d '{"data_view":{"title":"practix-logs-*","name":"practix logs","timeFieldName":"@timestamp"}}'
```

Примеры запросов KQL в Discover:

```
service: "nginx" and status >= 500
request_id: "3ec1f8b4-..."
service: "auth-service" and level: "ERROR"
```

## Раскладка файлов

```
infra/elk/
├── filebeat/filebeat.yml                 # что читать, что отбросить, куда слать
├── logstash/
│   ├── config/logstash.yml               # настройки самого Logstash (очередь, DLQ)
│   └── pipeline/logs.conf                # input / filter / output
└── scripts/apply_index_template.sh       # типы полей; одноразовый elasticsearch-logs-init
```

У Kibana конфига нет намеренно: образ маппит `ELASTICSEARCH_HOSTS` и остальное
из переменных окружения, и отдельный `kibana.yml` только продублировал бы блок
`environment:` в compose (тот же подход, что у Grafana).
