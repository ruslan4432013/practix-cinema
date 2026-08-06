# Websocket-шлюз мгновенных уведомлений

Раздаёт по открытым соединениям то, что сервис нотификаций уже доставил, и
деградирует на long polling, когда сокет не поднимается.

Проектное обоснование — [`docs/websockets.md`](../../docs/websockets.md).

## Почему отдельный сервис

Теория спринта: «Websocket стоит использовать только с асинхронными
инструментами. Многопоточный сервер рассчитан на быструю обработку запроса и
закрытие соединения, а каждое постоянно открытое соединение заблокирует поток
выполнения». Панель нотификаций — Django на gunicorn с двумя синхронными
воркерами: сто открытых вкладок заняли бы их все.

Здесь FastAPI + uvicorn, восьмой `--target` общего Dockerfile. Своей базы нет,
состояние — только реестр соединений в памяти процесса.

## Ручки

| Метод | Путь | Назначение |
|---|---|---|
| `POST` | `/api/v1/ws/ticket` | Одноразовый ticket + политика деградации (`Authorization: Bearer`) |
| `WS` | `/api/v1/ws?ticket=…` | Поток уведомлений. Ступень 1 |
| `GET` | `/api/v1/ws/poll?wait=25` | Long polling. Ступень 2 (`Authorization: Bearer`) |
| `GET` | `/health/live` | Жив ли процесс. Зависимостей не трогает |
| `GET` | `/health/ready` | Брокер + Redis + число соединений |
| — | `/api/ws/openapi` | Swagger (`/api/openapi`, `/api/analytics/openapi` и `/api/ugc/openapi` заняты) |

Ступень 3 — **не здесь**: догон идёт по ленте кабинета
`GET /api/v1/notifications/me/messages?since=…` в сервисе нотификаций. Она
единственная долговечная; шлюз не хранит ничего.

Nginx перед сервисом нет: он резолвит upstream'ы на старте и падает без
профильного контейнера, глобальный `proxy_set_header Connection ""` ломает
handshake, а `proxy_read_timeout 5s` рвал бы соединение каждые пять секунд.
Публикуется прямым хост-портом `NOTIFICATIONS_WS_HOST_PORT` (8091).

## Кадры

Все кадры — объект с полем `type`, чтобы клиент писал один `switch`.

| `type` | Когда | Содержимое |
|---|---|---|
| `hello` | сразу после `accept` | `user_id`, `ping_interval` |
| `notification` | пришло уведомление | `data`: `task_id`, `subject`, `preview`, `sent_at` |
| `desync` | буфер соединения переполнялся | `dropped` — сколько кадров потеряно, надо догнать ленту |
| `ping` / `pong` | периодически | — |

Тела письма в кадре нет: рассылка на сто тысяч человек — это сто тысяч копий
одного HTML. За подробностями клиент идёт в ленту по `task_id`.

## Авторизация

Токен на handshake **не передаётся**: браузер не умеет ставить заголовки в
`new WebSocket()`, а JWT в query-строке навсегда оседает в access-логах. Вместо
него одноразовый ticket, который гасится атомарным `GETDEL` при первом
использовании.

Три рубежа: `Origin` (у websocket нет same-origin policy), одноразовый ticket,
и периодическая перепроверка отзыва по денилисту Auth — выход из системы
закрывает открытые вкладки. Политика при недоступности Redis — `deny`.

Коды закрытия из приватного диапазона: `4401` — идти за новым токеном,
`4403` — чужой Origin, `4429` — лимит соединений, `4503` — шлюз не готов.

Коды ОТКАЗА у HTTP-ручек: `401` — заголовка нет вовсе (`MissingTokenError`),
**`422`** — подпись не сходится или токен просрочен. Второе выглядит странно, но
это код самой `async_fastapi_jwt_auth`, который отдаёт общий обработчик
`practix_core.jwt.install_exception_handler`, и он одинаков у Auth, коллектора,
UGC API и шлюза. Переопределить его здесь значило бы развести четыре сервиса по
кодам ответа ради одной ручки.

## Проверить руками

```bash
docker compose --env-file .env -f infra/compose/docker-compose.yml \
  --project-directory infra/compose --profile notifications up -d --build

TOKEN=$(curl -s -X POST localhost/api/v1/auth/login -H 'Content-Type: application/json' \
  -d '{"login":"ivan","password":"Str0ng-Pass!23"}' | jq -r .access_token)

# Ticket приезжает вместе с политикой деградации
curl -s -X POST localhost:8091/api/v1/ws/ticket -H "Authorization: Bearer $TOKEN" | jq

TICKET=$(curl -s -X POST localhost:8091/api/v1/ws/ticket -H "Authorization: Bearer $TOKEN" | jq -r .ticket)
websocat "ws://localhost:8091/api/v1/ws?ticket=$TICKET"      # держим открытым

# В другом окне — свободное сообщение в websocket-канал
curl -X POST localhost:8090/api/v1/notifications/messages \
  -H "X-Internal-Token: $NOTIFY_INTAKE_TOKEN" -H 'Content-Type: application/json' \
  -d '{"type":"websocket","user_id":"<uuid>","event_id":"demo-1","subject":"Привет","text":"Тест"}'

# Ticket одноразовый: тем же — уже нельзя
websocat "ws://localhost:8091/api/v1/ws?ticket=$TICKET"      # HTTP 403
websocat "ws://localhost:8091/api/v1/ws"                     # HTTP 403

# Ступень 2: то же уведомление без сокета
curl -s "localhost:8091/api/v1/ws/poll?wait=25" -H "Authorization: Bearer $TOKEN" | jq
```

Витрина деградации целиком — **http://localhost:8090/demo/cabinet**: вставить
токен, посмотреть на индикатор транспорта, затем
`docker compose ... stop notifications-ws` и увидеть переход на long polling, а
затем на ленту.

## Команды

```bash
npx nx run notifications-ws:test      # юнит-тесты, без инфраструктуры
npx nx run notifications-ws:docker    # сборка образа
```

Цели `test-functional` у проекта **нет**, и это решение, а не пропуск. Сквозной
путь уведомления (приём → веер → сборка → `WebsocketSender` → fanout → сокет)
проверяется набором нотификаций (`apps/notifications/tests/functional/test_websocket.py`),
где стенд уже стоит целиком — с Auth, брокером, Redis денилиста и четырьмя
контейнерами. Второй набор означал бы копию его `conftest.py` на четыреста с
лишним строк, а порог дублирования в репозитории жёсткий.

## Настройки

Префикс `NOTIFY_WS_` (голый `NOTIFY_` принадлежит панели и воркерам — корневой
`.env` читают все сразу). Полный список с обоснованиями — в `.env.example`.

Три группы имён намеренно без префикса, потому что описывают чужие сущности и
обязаны совпадать байт в байт у всех, кто их читает: `AUTHJWT_SECRET_KEY`
(секрет Auth, имя задано библиотекой), `AUTH_REDIS_*` (экземпляр Redis, в
котором Auth ведёт денилист) и `NOTIFY_AMQP_URL` (брокер нотификаций — своей
переменной под него нет намеренно).

## Не сделано (осознанно)

- **Чат между пользователями.** Урок предлагает мини-мессенджер как способ
  познакомиться с протоколом; здесь у websocket другая роль.
- **`wss://`.** Стенд без TLS целиком.
- **Метрики Prometheus.** Как и у сервиса нотификаций, своего `/metrics` нет;
  число соединений отдаёт `/health/ready`.
- **Несколько воркеров uvicorn.** `--workers 1`: реестр соединений живёт в
  памяти процесса, и второй воркер считал бы лимиты и счётчики по половине
  соединений. Масштабирование — репликами контейнера, у которых схема раздачи
  через fanout та же, а счётчики честные.
