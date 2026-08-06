"""Настройки для юнит-тестов админки: SQLite в памяти, без внешней инфраструктуры.

Нужны потому, что `npx nx run-many -t test` выполняется на голом раннере, где
Postgres нет. Тот же приём и по той же причине, что в
`practix_notifications/settings_test.py`, — но этот сервис живёт ВНЕ uv-workspace
(Python 3.12, свой `uv.lock`), поэтому оттуда ничего не импортируется.

## Про схему `content` и `db_table = 'content"."genre'`

Кавычка в имени таблицы — это инъекция схемы: Django оборачивает имя в кавычки,
и на Postgres получается `"content"."genre"`, то есть таблица `genre` в схеме
`content`. На SQLite схем нет, и та же строка даёт одну таблицу с длинным
странным именем — синтаксически валидным, поэтому `migrate` проходит, а тесты
моделей работают. Это единственная причина, по которой набор вообще может идти
на SQLite: если в моделях когда-нибудь появится постгресовое поле
(`ArrayField`, `JSONField` из `django.contrib.postgres`), юнит-тесты молча
станут функциональными и здесь придётся честно поднимать Postgres.
"""

from example.settings import *  # noqa: F403 - это переопределение, а не самостоятельный конфиг

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}

# Хеширование пароля — самая дорогая операция в юнит-тестах Django и
# единственная, которая здесь ничего не проверяет.
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']

# Манифест статики требует собранного collectstatic, которого в тестах нет.
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}

# Наблюдаемость в тестах выключена целиком: OTel-экспортёр стучался бы в Jaeger
# на каждый запрос, Sentry — в GlitchTip, а ни того ни другого на раннере нет.
OTEL_ENABLED = False
SENTRY_ENABLED = False

# Продюсер события «новый фильм» ВЫКЛЮЧЕН по умолчанию, как и на стенде: тесты,
# которым он нужен, включают его сами через `override_settings`. Обратный
# порядок означал бы, что любой тест, сохраняющий фильм, шлёт HTTP-запрос.
NOTIFY_EVENTS_ENABLED = False
NOTIFY_API_URL = 'http://notifications-admin:8000'
NOTIFY_INTAKE_TOKEN = 'test-internal-token'
