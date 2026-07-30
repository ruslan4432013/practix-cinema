# Auth Service

Сервис аутентификации и авторизации на FastAPI + PostgreSQL + Redis.
Код расположен под `auth/src/` (это корень пакета: `PYTHONPATH=/opt/app/auth/src`,
запуск — `uvicorn src.main:app` из каталога `auth/`). Конфигурация — через
**pydantic-settings** (поля в стиле `UPPER_SNAKE_CASE`, загрузка из `.env`).

## Возможности

- Регистрация и аутентификация пользователей (JWT access + refresh).
- Ротация refresh-токенов, denylist в Redis.
- Выход с текущего устройства и со всех устройств.
- История входов пользователя (с определением типа устройства).
- Управление ролями (CRUD) и назначение/отзыв ролей у пользователей.
- Bypass проверки прав для суперпользователя (роли `admin` / `superuser`).
- Эндпоинт **`POST /api/v1/users/check-permissions`** для межсервисной проверки прав.
- Эндпоинты `GET /api/v1/users/me` и `PATCH /api/v1/users/me/credentials`
  для получения профиля и смены логина/email.
- Вход через соцсети (OAuth, Яндекс) и управление привязанными аккаунтами
  в личном кабинете: список и открепление (со страховкой от блокировки входа).
- Распределённая трассировка (OpenTelemetry → Jaeger) и сквозной `X-Request-Id`.
- Ограничение частоты запросов (rate limiting) на Redis.
- CLI-команда `createsuperuser` для создания суперпользователя.

## Архитектура

Слои: `api/v1/` (роутеры) → `services/` (бизнес-логика) → `models/` (ORM + Pydantic-схемы).

```
auth/
├── src/
│   ├── api/v1/        # роутеры: auth, users, roles, oauth, dependencies
│   ├── core/          # config (pydantic-settings), tracing, rate_limit, request_id
│   ├── db/            # подключения PostgreSQL / Redis
│   ├── models/        # ORM-модели (entity.py) и Pydantic-схемы (schemas.py)
│   ├── services/      # auth_service, role_service, oauth_service, security, user_agent
│   ├── cli.py         # CLI-утилиты (createsuperuser)
│   └── main.py        # точка входа FastAPI
├── migrations/        # Alembic-миграции (+ docs/partitioning.md)
└── Dockerfile
```

Порядок middleware в `main.py` (Starlette выполняет их в обратном порядке
регистрации): `init_tracer_provider()` → `RequestIdMiddleware` →
`RateLimitMiddleware` → `instrument_app` (OTel FastAPI).

## Модель аутентификации и сессий

- **JWT** через `async_fastapi_jwt_auth` (`AuthJWT`). Роли пользователя
  вшиты в access-токен как claim `roles` — авторизация читает их из токена,
  а не из БД (быстрее); суперпользовательские роли (`SUPERUSER_ROLES`,
  по умолчанию `{admin, superuser}`) проходят любую проверку.
- На логине access- и refresh-JTI регистрируются в Redis-множестве
  `user:{id}:sessions`. Logout ревокирует текущий JTI, logout-all — все JTI
  сессии. Refresh **ротирует** refresh-токен (старый ревокируется).
  Denylist проверяется через `token_in_denylist_loader` (Redis).
- Пароли хешируются `pwdlib` (argon2 приоритетно, bcrypt как fallback);
  политика надёжности: ≥8 символов, буквы разного регистра, цифра, спецсимвол.
  Пользователи, созданные только через OAuth, могут не иметь пароля
  (`password` в БД — nullable).

## Сквозные возможности спринта 2

### Трассировка → Jaeger
`core/tracing.py` (`init_tracer_provider` + `instrument_app`) экспортирует
OpenTelemetry-спаны по OTLP/HTTP в Jaeger. Управляется переменными
`OTEL_ENABLED`, `OTEL_EXPORTER_OTLP_ENDPOINT` (по умолчанию `http://jaeger:4318`),
`OTEL_SERVICE_NAME=auth-service`. Трейсы доступны в UI `http://localhost:16686`.

### X-Request-Id
`core/request_id.py` (`RequestIdMiddleware`) **требует** заголовок `X-Request-Id`
(400, если отсутствует), проставляет его в активный span и возвращает в ответе.
В проде заголовок формирует Nginx; при прямых обращениях к сервису его нужно
передавать самостоятельно.

### Rate limiting
`core/rate_limit.py` (`RateLimitMiddleware`) — фиксированное окно на Redis.
Настройки: `RATE_LIMIT_ENABLED` (по умолчанию `True`),
`RATE_LIMIT_TIMES` (20), `RATE_LIMIT_SECONDS` (60).

### OAuth (Яндекс) и привязка соцсетей
Реализована только сторона потребителя (authorization code flow). Роутер
`/api/v1/oauth` монтируется **только при `OAUTH_ENABLED=True`**.
CSRF-параметр `state` хранится в Redis (`oauth:state:{state}`, TTL `OAUTH_STATE_TTL=300`).
Поток: `code → токен Яндекса → профиль → поиск/связывание/создание пользователя → наши JWT`.
Привязки хранятся в таблице `social_accounts` (уникальность `provider + provider_user_id`).
Открепление соцсети (`DELETE /social/accounts/{id}`) защищено от блокировки:
нельзя открепить последний способ входа у пользователя без пароля (409).
Конфигурация: `YANDEX_CLIENT_ID/SECRET`, `YANDEX_REDIRECT_URI`,
`YANDEX_AUTHORIZE_URL/TOKEN_URL/USERINFO_URL`.

### Партиционирование `login_history`
Таблица истории входов — двухуровневая секционированная в PostgreSQL:
составной PK `(id, auth_date, device_type)`, RANGE по `auth_date` (по годам) →
LIST по `device_type` (`web` / `mobile` / `smart` + DEFAULT `_other`),
индекс `(user_id, auth_date DESC)`. Тип устройства определяется по User-Agent
(`services/user_agent.py`, библиотека `user-agents`). Такая схема ускоряет
выборку истории по времени и разделяет данные по классам устройств на горизонте
в миллионы записей. Подробнее — `migrations/docs/partitioning.md`.

## Модели данных (`models/entity.py`)

- **User** — `id` (UUID), `login` (unique), `password` (**nullable**), `email` (unique),
  `created_at`; связи: `roles` (M2M, `lazy="selectin"`), `login_histories`, `social_accounts`.
- **Role** — `id`, `name` (unique), `description`; M2M через `user_roles`.
- **LoginHistory** — секционированная (см. выше); `user_agent`, `ip_address`, `device_type`.
- **SocialAccount** — `provider`, `provider_user_id` (уникальная пара), `user_id`.

## Миграции

Alembic (`auth/migrations/`); сервис `auth-migrations` в compose выполняет
`alembic upgrade head` до старта `auth`. Цепочка ревизий:

1. `276725a5c545` — начальная (roles, users, login_history RANGE, user_roles).
2. `a1b2c3d4e5f6` — `users.password` → nullable, таблица `social_accounts`.
3. `b2c3d4e5f6a7` — перестройка `login_history` в двухуровневую RANGE→LIST по устройству.

Локально: `cd auth && alembic upgrade head` (или `alembic revision --autogenerate -m "..."`).

## Конфигурация (основные переменные окружения)

| Группа        | Переменные                                                                 |
|---------------|----------------------------------------------------------------------------|
| БД            | `AUTH_POSTGRES_DB/USER/PASSWORD/HOST/PORT`                                  |
| Redis         | `REDIS_HOST`, `REDIS_PORT`                                                  |
| JWT           | `AUTHJWT_SECRET_KEY`, `AUTHJWT_DENYLIST_ENABLED`, `ACCESS_TOKEN_EXPIRES`, `REFRESH_TOKEN_EXPIRES` |
| Роли          | `DEFAULT_ROLE_NAME`, `SUPERUSER_ROLES`                                      |
| Rate limiting | `RATE_LIMIT_ENABLED`, `RATE_LIMIT_TIMES`, `RATE_LIMIT_SECONDS`              |
| Трассировка   | `OTEL_ENABLED`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME`          |
| OAuth         | `OAUTH_ENABLED`, `YANDEX_CLIENT_ID/SECRET`, `YANDEX_REDIRECT_URI`, `OAUTH_STATE_TTL` |

`entrypoint.sh` копирует `.env.example` → `.env` внутри контейнера, если файла нет.

## Запуск

```bash
docker-compose up -d --build
```

API и Swagger UI доступны по адресу `http://localhost:<port>/api/openapi`.

## CLI-команды

### Создание суперпользователя

```bash
docker-compose exec auth python -m src.cli createsuperuser \
    --login admin \
    --email admin@example.com
```

Команда идемпотентна: при повторном вызове пользователь будет повышен до
суперпользователя, а роль `admin` создана, если её ещё нет.

## Основные эндпоинты

| Метод | Путь                                  | Описание                              |
|-------|---------------------------------------|---------------------------------------|
| POST  | `/api/v1/auth/register`               | Регистрация                           |
| POST  | `/api/v1/auth/login`                  | Вход                                  |
| POST  | `/api/v1/auth/refresh`                | Обновление токенов                    |
| POST  | `/api/v1/auth/logout`                 | Выход                                 |
| POST  | `/api/v1/auth/logout-all`             | Выход на всех устройствах             |
| POST  | `/api/v1/auth/change-password`        | Смена пароля                          |
| GET   | `/api/v1/auth/login-history`          | История входов                        |
| GET   | `/api/v1/users/me`                    | Профиль текущего пользователя         |
| PATCH | `/api/v1/users/me/credentials`        | Смена логина/email                    |
| POST  | `/api/v1/users/check-permissions`     | Межсервисная проверка прав            |
| POST  | `/api/v1/roles/`                      | Создание роли (admin)                 |
| GET   | `/api/v1/roles/`                      | Список ролей (admin)                  |
| POST  | `/api/v1/roles/assign`                | Назначить роль пользователю (admin)   |
| POST  | `/api/v1/roles/remove`                | Отозвать роль (admin)                 |
| PATCH | `/api/v1/roles/{role_id}`             | Изменить роль (admin)                 |
| DELETE| `/api/v1/roles/{role_id}`             | Удалить роль (admin)                  |
| GET   | `/api/v1/oauth/yandex/login`          | Начать вход через Яндекс              |
| GET   | `/api/v1/oauth/yandex/callback`       | Callback Яндекс OAuth                 |
| GET   | `/api/v1/oauth/social/accounts`       | Список привязанных соцсетей           |
| DELETE| `/api/v1/oauth/social/accounts/{id}`  | Открепить аккаунт соцсети             |

> Роутер `/api/v1/oauth` доступен только при `OAUTH_ENABLED=True`.

## Тесты

Функциональные тесты находятся в каталоге `tests/functional/` (Auth-тесты —
`tests/functional/auth`, запускаются in-process через httpx `ASGITransport`
против реальной `auth-db`):

```bash
cd tests/functional
docker-compose up --build --abort-on-container-exit
```

## Changelog

См. файл [CHANGELOG.md](./CHANGELOG.md).
