# Django Admin

Панель администратора для управления данными онлайн-кинотеатра. Позволяет добавлять, редактировать и удалять фильмы, жанры и персон. Данные хранятся в схеме `content` PostgreSQL. Кастомная модель пользователя — `users.User` (`AUTH_USER_MODEL`).

## Конфигурация

Настройки, зависящие от окружения, вынесены в `example/config.py` — класс `Settings(BaseSettings)` на базе **pydantic-settings** (поля в `UPPER_SNAKE_CASE`, читаются из окружения или файла `.env`). `settings.py` берёт все env-значения из него (`SECRET_KEY`, `DEBUG`, параметры БД `SQL_*`/`POSTGRES_*`, `AUTH_API_*`, `OTEL_*`). Собственно константы Django-фреймворка (`INSTALLED_APPS`, `TEMPLATES`, `MIDDLEWARE` и т. п.) остаются обычными переменными модуля.

## Запуск локально

1. Убедитесь, что у вас установлен Docker и Docker Compose.
2. Перейдите в директорию `django_admin`.
3. Запустите сервисы:
   ```bash
   docker-compose up -d
   ```
   Это поднимет базу данных Postgres, само приложение Django и Nginx.

4. При первом запуске автоматически выполнятся миграции и создастся суперпользователь (если заданы соответствующие переменные окружения).

5. Панель будет доступна по адресу: `http://localhost/admin`

## Аутентификация через Auth-сервис (SSO)

Вход в админку выполняется через внешний Auth-сервис. Порядок бэкендов в `settings.py` задан намеренно:

```python
AUTHENTICATION_BACKENDS = [
    'users.auth_backend.AuthServiceBackend',
    'django.contrib.auth.backends.ModelBackend',
]
```

`AuthServiceBackend` (`users/auth_backend.py`):

1. POST `{AUTH_API_URL}/api/v1/auth/login` с телом `{login, password}`.
2. GET `{AUTH_API_URL}/api/v1/users/me` с заголовком `Authorization: Bearer <access_token>`.
3. Маппит роли Auth в права Django: роли из `SUPERUSER_ROLES`/`STAFF_ROLES` (`{admin, superuser}`) дают `is_superuser`/`is_staff`.
4. Синхронизирует локального пользователя через `User.objects.update_or_create(id=...)`.

**Изящная деградация.** Все вызовы к Auth идут через `_request_with_retry`: ограниченное число попыток (`AUTH_API_MAX_ATTEMPTS`, по умолчанию 3), экспоненциальная задержка (стартовая 0.1s, удваивается) и таймаут (`AUTH_API_TIMEOUT`, по умолчанию 2.0s). Если Auth недоступен (таймаут, сеть, `RequestException`), `authenticate` возвращает `None`, и Django переходит к `ModelBackend` — локальный суперпользователь войдёт даже при лежащем Auth. Падение Auth не выводит админку из строя.

Идентификатор запроса `X-Request-Id` (в проде его проставляет Nginx, читается из `request.META['HTTP_X_REQUEST_ID']`) прокидывается заголовком на все исходящие вызовы в Auth.

## Трассировка (OpenTelemetry → Jaeger)

Инициализация — в `example/wsgi.py` (`_init_tracing()`), поэтому трассировка поднимается уже после fork'а воркеров uWSGI; `manage.py`/миграции `wsgi.py` не импортируют и остаются без трассировки. Экспорт по OTLP/HTTP в `${OTEL_EXPORTER_OTLP_ENDPOINT}/v1/traces` (по умолчанию `http://jaeger:4318`), инструментируются `DjangoInstrumentor` (серверные span'ы + извлечение `traceparent`) и `RequestsInstrumentor` (исходящие вызовы в Auth). Включается флагом `OTEL_ENABLED`, имя сервиса — `OTEL_SERVICE_NAME` (`django-admin`).

`example/middleware.py` `RequestIdMiddleware` требует заголовок `X-Request-Id` (иначе `400`), проставляет его тегом `http.request_id` на активный span и возвращает клиенту в ответном заголовке.

## Movies content API

Внутренний read-only JSON API контента под `movies/api/v1`: `MoviesListApi` (пагинация по 50) и `MoviesDetailApi`, агрегирующие связанные жанры/персон через `ArrayAgg`. Работает поверх таблиц схемы `content`.

## Переменные окружения

Для работы сервиса необходим файл `.env` в директории `django_admin` со следующими переменными:

| Переменная | Описание |
|------------|----------|
| `POSTGRES_DB` | Имя базы данных Postgres |
| `POSTGRES_USER` | Имя пользователя Postgres |
| `POSTGRES_PASSWORD` | Пароль пользователя Postgres |
| `SECRET_KEY` | Секретный ключ Django |
| `DEBUG` | Режим отладки (True/False) |
| `DJANGO_SUPERUSER_USERNAME` | Имя суперпользователя (для автосоздания) |
| `DJANGO_SUPERUSER_PASSWORD` | Пароль суперпользователя |
| `DJANGO_SUPERUSER_EMAIL` | Email суперпользователя |
| `SQL_HOST` | Хост БД (по умолчанию 127.0.0.1) |
| `SQL_PORT` | Порт БД (по умолчанию 5432) |
| `SQL_OPTIONS` | Дополнительные опции подключения к БД |
| `AUTH_API_URL` | URL Auth-сервиса (по умолчанию `http://auth:8000`) |
| `AUTH_API_TIMEOUT` | Таймаут вызовов Auth в секундах (по умолчанию 2.0) |
| `AUTH_API_MAX_ATTEMPTS` | Число попыток при ошибке Auth (по умолчанию 3) |
| `OTEL_ENABLED` | Включение трассировки OpenTelemetry (по умолчанию True) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Endpoint OTLP-коллектора (по умолчанию `http://jaeger:4318`) |
| `OTEL_SERVICE_NAME` | Имя сервиса в трассировке (по умолчанию `django-admin`) |

> Значения по умолчанию и типы полей описаны в `example/config.py`. В зависимостях появились `pydantic` и `pydantic-settings`.
