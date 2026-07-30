# Функциональные тесты Async API

Интеграционные тесты, проверяющие поведение сервисов «от края до края» на реальной инфраструктуре (Elasticsearch, Redis, PostgreSQL), поднимаемой через Docker Compose. Разделены на два набора:

- **`rest/`** — тесты Movies API. Данные загружаются в ES через Bulk API, ответы проверяются через HTTP-клиент aiohttp (сервис поднят отдельным контейнером).
- **`auth/`** — тесты Auth-сервиса. Приложение запускается **in-process** через `httpx` `ASGITransport` поверх реальной `auth-db` (PostgreSQL) и Redis.

Во всех наборах `asyncio_mode = auto` (pytest-asyncio), поэтому async-тестам и фикстурам декораторы не нужны, и `asyncio_default_fixture_loop_scope = session` — один event loop на прогон. Раньше то же самое делалось переопределением фикстуры `event_loop`; этот приём удалён в pytest-asyncio 1.0 и молча перестал бы работать.

Быстрые проверки чистых функций вынесены в отдельный набор [`tests/unit/`](../unit/README.md): им не нужна ни Kafka, ни Redis, ни Docker.

---

## Что тестируется (Movies API, `rest/`)

| Файл | Endpoint | Тест-классы |
|------|----------|-------------|
| `test_film.py` | `/api/v1/films` | `TestFilmsList` — список, пагинация, сортировка, фильтр по жанру, валидация |
| | `/api/v1/films/search` | `TestFilmSearch` — поиск, пагинация, пустой результат |
| | `/api/v1/films/{id}` | `TestFilmDetails` — карточка, 404, 422 |
| | | `TestFilmCache` — кэширование в Redis |
| `test_genre.py` | `/api/v1/genres` | `TestGenresList` — список, пустой индекс |
| | `/api/v1/genres/{id}` | `TestGenreDetails` — карточка, 404, 422 |
| | | `TestGenreCache` — кэширование списка и карточки |
| `test_person.py` | `/api/v1/persons/search` | `TestPersonSearch` — поиск, роли, пагинация |
| | `/api/v1/persons/{id}` | `TestPersonDetails` — карточка с фильмографией |
| | `/api/v1/persons/{id}/film` | `TestPersonFilms` — фильмы персоны |
| | | `TestPersonCache` — кэширование |
| `test_search.py` | `/api/v1/films/search` | `TestFilmSearch` — поиск по title и description, пагинация, кэш |
| | `/api/v1/persons/search` | `TestPersonSearch` — поиск по имени, роли actor/director, кэш |

**Итого: 67 тестов.**

---

## Что тестируется (Auth-сервис, `auth/`)

Тесты Auth запускают приложение in-process (`httpx` `ASGITransport`) против реальной `auth-db` и Redis. Каждый тест пересоздаёт схему БД (`drop_all`/`create_all` в `conftest.py`), сессия и Redis подменяются через `app.dependency_overrides`.

| Файл | Что покрывает |
|------|---------------|
| `test_auth.py` | Регистрация, логин, refresh с ротацией токена, logout / logout-all, денилист JWT |
| `test_users_and_roles.py` | CRUD ролей, назначение/снятие ролей, права по ролям |
| `test_login_history_pagination.py` | Пагинация истории входов |
| `test_login_history_partition.py` | RANGE-партиционирование `login_history` (секции создаются листенером `after_create`) |
| `test_oauth_yandex.py` | OAuth-логин через Yandex (потребительская сторона; `OAUTH_ENABLED=True`) |
| `test_rate_limit.py` | Ограничение частоты запросов (счётчики `ratelimit:*` в Redis) |

> **X-Request-Id.** Сервисы теперь требуют заголовок `X-Request-Id` (в проде его ставит Nginx). Тестовый `client` в `auth/conftest.py` шлёт его на всех запросах (`headers={"X-Request-Id": "test-request-id"}`) из-за `RequestIdMiddleware`. Клиенты, ходящие в сервис напрямую, минуя Nginx, должны проставлять этот заголовок сами.
>
> **Rate limit.** Все тесты ходят в один Redis с одного тестового IP, поэтому фикстура `client` очищает ключи `ratelimit:*` перед каждым тестом, чтобы общий счётчик не «протекал» между тестами.

---

## Что тестируется (Analytics Collector, `analytics_collector/`)

Приложение запускается in-process (`httpx` `ASGITransport`) против **настоящих** Kafka и Redis. Kafka в тестовом стеке — один брокер с RF=1: тесты проверяют логику сервиса, а не отказоустойчивость кластера.

| Файл | Что покрывает |
|------|---------------|
| `test_events.py` | Приём всех типов событий и их попадание в правильный топик с правильным ключом партиционирования и заголовками; батч; дедупликация по `event_id`; расчёт `completion_rate` на сервере |
| `test_auth.py` | `user_id` берётся только из подписи токена; невозможность подделать `user_id` / `is_authenticated` / `received_at` через тело; чужая подпись, просроченный и битый токен → событие принимается как анонимное |
| `test_validation.py` | `extra='forbid'`, закрытые перечисления, схемы URL (`javascript:` / `data:`), плоскость и размер `properties`, лимиты пачки, ограничение размера тела (413), вменяемость клиентских меток времени |
| `test_degradation.py` | Буферизация при недоступной Kafka (202 вместо 5xx), дренаж после восстановления, сохранность записей при отказе брокера в середине дренажа, восстановление зависших in-flight записей, работа при недоступном Redis |
| `test_privacy_and_ops.py` | Отсутствие сырого IP в сообщении, стабильность и солёность `ip_hash`, усечение User-Agent; rate limit, включая пропорциональный расход лимита пачкой; пробы здоровья и метрики; сквозной `X-Request-Id` |

> **Брокер-заглушка.** Сценарии деградации используют `StubBroker` из `conftest.py`, который «падает» по команде теста. Останавливать настоящий брокер в середине прогона значило бы сделать тесты медленными и зависимыми друг от друга.
>
> **Чтение из Kafka.** Топики между тестами не пересоздаются (это дорого), поэтому фикстура `kafka_reader` выдаёт каждому тесту свою consumer-группу и читает с начала, а тесты находят своё сообщение по `event_id`. Работает, но с ростом числа тестов чтение с начала топика будет замедляться. Когда это станет заметно, есть два выхода: уникальный суффикс топика на прогон (`ugc.clicks.v1.test-<uuid>` через переопределение `KAFKA_TOPIC_*`) либо чтение с конца партиции с запоминанием оффсета до отправки события.
>
> **X-Request-Id здесь не обязателен** — в отличие от `rest/` и `auth/`. Ручка публичная и вызывается через `navigator.sendBeacon`, который не умеет ставить заголовки, поэтому middleware генерирует идентификатор, а не отклоняет запрос.

---

## Стек

| Инструмент | Версия | Назначение |
|------------|--------|------------|
| pytest | 9.1.0 | Тест-раннер |
| pytest-asyncio | 1.4.0 | Поддержка async тестов и фикстур |
| aiohttp | (из приложения) | HTTP-клиент для запросов к API |
| Elasticsearch | (из приложения) | Клиент для подготовки тестовых данных |
| Redis | (из приложения) | Проверка кэширования |

Версии закреплены в корневом `constraints.txt` и приезжают **в образе** — ни
один entrypoint больше ничего не доустанавливает. Раньше сервис `tests` понижал
pytest прямо в контейнере до 7.4.3, а `auth-tests` ставил его вовсе без
закрепления версии, так что каждый прогон мог получить другой раннер. Между
pytest-asyncio 0.21 и 1.x изменилась семантика `asyncio_mode=auto` и области
видимости `event_loop`, поэтому набор, проходящий локально, мог падать в CI.

---

## Запуск через Docker Compose (рекомендуется)

```bash
# Из корня репозитория
docker compose -f tests/functional/docker-compose.yml \
               --project-directory tests/functional \
               up --build --abort-on-container-exit --exit-code-from tests
```

Docker Compose поднимает:
1. **elasticsearch** — ES 9.4.1 (с healthcheck)
2. **redis** — Redis 7 alpine (с healthcheck) для Movies API (кэш) и Auth (денилист/сессии/rate limit)
3. **redis-ugc** — отдельный экземпляр под набор `ugc`, как и в боевом compose. Раньше наборы делили один контейнер, разведённые лишь номером базы: это работало, но держалось на том, что никто не напишет `flushall` вместо `flushdb`
4. **fastapi** — собирается из корневого `Dockerfile`, ждёт healthy ES и Redis
5. **tests** — Movies API: ждёт готовности ES/Redis и запускает `pytest /tests/functional/rest`
6. **auth-db** — PostgreSQL 16 для тестов Auth (с healthcheck)
7. **auth-tests** — собирается из `auth/Dockerfile`, ждёт healthy `auth-db` и Redis, запускает `pytest /tests/functional/auth` (с `OAUTH_ENABLED=True`)
8. **kafka** — один брокер KRaft (с healthcheck), RF=1
9. **kafka-init-test** — создаёт топики UGC тем же скриптом, что и продовый `kafka-init`, но с `REPLICATION_FACTOR=1`
10. **ugc-tests** — собирается из `analytics_collector/Dockerfile`, ждёт готовности топиков (`wait_for_kafka.py`), запускает `pytest /tests/functional/ugc`
11. **clickhouse** / **clickhouse-init-test** / **toxiproxy** / **etl-clickhouse** / **etl-tests** — стенд ETL

Отдельные наборы запускаются через `run --rm`, а НЕ через
`up --abort-on-container-exit`: последний валит весь стек, как только завершается
любой контейнер, включая одноразовые `kafka-init-test` и `clickhouse-init-test`.

```bash
docker compose -f tests/functional/docker-compose.yml \
               --project-directory tests/functional run --rm --build ugc-tests

docker compose -f tests/functional/docker-compose.yml \
               --project-directory tests/functional run --rm --build auth-tests

docker compose -f tests/functional/docker-compose.yml \
               --project-directory tests/functional run --rm --build etl-tests
```

> Между наборами нужен `down -v`: тесты ETL намеренно публикуют битые
> сообщения (проверка карантина), и без очистки томов они попадут в выборку
> тестов коллектора.

В CI (`.github/workflows/ci.yml`) четыре набора идут параллельно отдельными job'ами.

---

## Запуск локально (без Docker)

Необходимо иметь запущенные Elasticsearch 9.x и Redis, а также сам FastAPI-сервис.

```bash
# 1. Создать и активировать виртуальное окружение
python -m venv .venv && source .venv/bin/activate

# 2. Установить зависимости приложения и тестов
# pytest и pytest-asyncio уже входят в requirements.txt
pip install -r requirements.txt

# 3. Настроить переменные окружения (или создать tests/functional/.env)
export ES_HOST=http://127.0.0.1:9200
export REDIS_HOST=127.0.0.1
export REDIS_PORT=6379
export SERVICE_URL=http://127.0.0.1:8000

# 4. Запустить тесты
pytest tests/functional/rest -v     # Movies API
pytest tests/functional/auth -v     # Auth (нужны auth-db и Redis)
```

Тесты Auth не требуют отдельно запущенного сервиса (приложение поднимается in-process), но им нужны доступные `auth-db` (PostgreSQL) и Redis; параметры подключения берутся из окружения (`AUTH_POSTGRES_*`, `REDIS_HOST`/`REDIS_PORT`, см. `docker-compose.yml`).

---

## Переменные окружения для тестов

Настройки находятся в `tests/functional/settings.py` (класс `TestSettings`).
Все переменные можно переопределить через окружение или файл `.env` в корне проекта.

| Переменная | Значение по умолчанию | Описание |
|---|---|---|
| `ES_HOST` | `http://127.0.0.1:9200` | URL Elasticsearch |
| `REDIS_HOST` | `127.0.0.1` | Хост Redis |
| `REDIS_PORT` | `6379` | Порт Redis |
| `SERVICE_URL` | `http://127.0.0.1:8000` | URL FastAPI-сервиса |

Внутри Docker Compose эти переменные передаются сервису `tests` напрямую через `environment`.

---

## Архитектура тестов

```
tests/functional/
├── conftest.py          # Общие фикстуры (session-scoped: es_client, redis_client, http_session)
├── pytest.ini           # asyncio_mode = auto
├── settings.py          # TestSettings (pydantic-settings)
├── docker-compose.yml   # Тест-инфраструктура
├── rest/
│   ├── test_film.py     # Тесты /films
│   ├── test_genre.py    # Тесты /genres
│   ├── test_person.py   # Тесты /persons
│   └── test_search.py   # Тесты /films/search + /persons/search
├── auth/
│   ├── conftest.py                       # Фикстуры: пересоздание БД, in-process client с X-Request-Id
│   ├── test_auth.py                      # Логин, refresh, logout, денилист
│   ├── test_users_and_roles.py           # Пользователи и роли
│   ├── test_login_history_pagination.py  # Пагинация истории входов
│   ├── test_login_history_partition.py   # Партиционирование login_history
│   ├── test_oauth_yandex.py              # OAuth через Yandex
│   └── test_rate_limit.py                # Ограничение частоты запросов
├── testdata/
│   └── es_mapping.py    # ES-маппинги для movies, persons, genres
└── utils/
    ├── helpers.py             # make_film, make_genre, make_person, get_es_bulk_query
    ├── retry_utils.py         # backoff с ограничением числа попыток
    ├── wait_for_es.py         # Ожидание готовности ES
    ├── wait_for_redis.py      # Ожидание готовности Redis
    ├── wait_for_kafka.py      # Ожидание готовности топиков
    └── wait_for_clickhouse.py # Ожидание применения DDL хранилища
```

### Ключевые фикстуры

| Фикстура | Scope | Описание |
|----------|-------|----------|
| `es_client` | session | AsyncElasticsearch (создаётся один раз) |
| `redis_client` | session | Redis async client |
| `http_session` | session | aiohttp.ClientSession |
| `flush_redis` | function | Очищает Redis до и после теста |
| `es_movies_index` | function | Создаёт/удаляет индекс `movies` |
| `es_persons_index` | function | Создаёт/удаляет индекс `persons` |
| `es_genres_index` | function | Создаёт/удаляет индекс `genres` |
| `es_write_data` | function | Bulk-загрузка документов в ES |
| `make_get_request` | function | GET-запрос к API, возвращает `HTTPResponse` |
