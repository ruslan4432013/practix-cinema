"""Структурное логирование: JSON одной строкой, поля из ``extra=...`` как есть.

Модуль существовал в ЧЕТЫРЁХ копиях (`rest`, `auth`, `analytics_collector`,
`etl_clickhouse`), совпадавших примерно на 85 %: побайтово одинаковые
``_RESERVED_RECORD_FIELDS``, ``JsonFormatter.format``, ``RequestIdFilter`` и
скелет ``dictConfig``. pylint показывал между `rest` и `auth` 71 схожую строку.
Показать все четыре копии одним прогоном ни один детектор не мог: сервисы
раскладываются в одинаковые имена модулей ``src.core.logger`` и не попадают в
один проход — см. docs/monorepo.md.

## Почему здесь нет ни одного импорта настроек

``rest/core/config.py`` импортирует логгер и вызывает ``setup_logging()`` НА
ИМПОРТЕ. Если бы логгер, в свою очередь, импортировал настройки, получился бы
цикл — именно поэтому в `rest`/`auth` уровень и формат читались прямо из
``os.environ``, о чём в тех модулях стояло отдельное предупреждение.

Правило этого модуля: **импортируются только stdlib и ``practix_core.context``;
никаких настроек и никаких побочных эффектов на импорте.** Значения передаёт
вызывающая сторона, поэтому цикл невозможен по построению: `rest`/`auth` читают
окружение, коллектор и ETL — свои ``settings``.

Перенос самого вызова ``setup_logging()`` из ``core/config.py`` в ``lifespan`` —
правильное лечение причины, но это отдельная задача: он меняет МОМЕНТ вызова
``dictConfig`` относительно импорта настроек, то есть ровно тот класс правок,
который тихо меняет вывод логов на старте.
"""

import json
import logging
import os
import sys
from collections.abc import Mapping
from logging import config as logging_config

from practix_core.context import request_id_ctx

# Поля LogRecord, которые не являются пользовательскими и в JSON не нужны.
RESERVED_RECORD_FIELDS = frozenset(
    {
        'args', 'asctime', 'created', 'exc_info', 'exc_text', 'filename', 'funcName',
        'levelname', 'levelno', 'lineno', 'module', 'msecs', 'message', 'msg', 'name',
        'pathname', 'process', 'processName', 'relativeCreated', 'stack_info',
        'thread', 'threadName', 'taskName',
    }
)  # fmt: skip

VERBOSE_FORMAT_WITH_REQUEST_ID = '%(asctime)s - %(name)s - %(levelname)s - [rid=%(request_id)s] - %(message)s'
VERBOSE_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'


class RequestIdFilter(logging.Filter):
    """Добавляет в запись идентификатор текущего запроса."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get() or '-'
        return True


class JsonFormatter(logging.Formatter):
    """Форматирует запись в одну строку JSON.

    :param static_fields: поля, добавляемые в каждую запись. Так ETL и коллектор
        проставляют ``service``: у фонового процесса нет request_id, и без имени
        сервиса записи в общем хранилище логов неотличимы друг от друга.
    :param include_request_id: добавлять ли ``request_id``. Выключено у ETL — у
        консьюмера нет HTTP-запроса, и поле было бы всегда ``'-'``.

    ``dictConfig`` передаёт эти аргументы через лишние ключи в описании
    форматтера: ``{'()': JsonFormatter, 'include_request_id': False}``.
    """

    def __init__(
        self,
        *args,
        static_fields: Mapping[str, str] | None = None,
        include_request_id: bool = True,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.static_fields = dict(static_fields or {})
        self.include_request_id = include_request_id

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            'timestamp': self.formatTime(record, '%Y-%m-%dT%H:%M:%S%z'),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
        }
        if self.include_request_id:
            payload['request_id'] = getattr(record, 'request_id', '-')
        payload.update(self.static_fields)
        if record.exc_info:
            payload['exception'] = self.formatException(record.exc_info)
        # Всё, что передали через extra=..., попадает в JSON как есть.
        for key, value in record.__dict__.items():
            if key not in RESERVED_RECORD_FIELDS and key not in payload:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def build_logging_config(
    *,
    level: str = 'INFO',
    json_output: bool = True,
    static_fields: Mapping[str, str] | None = None,
    include_request_id: bool = True,
    logger_levels: Mapping[str, str | None] | None = None,
) -> dict:
    """Собирает словарь для ``logging.config.dictConfig``.

    :param logger_levels: уровни отдельных логгеров; ``None`` в значении означает
        «тот же уровень, что у root». Остаётся НА СТОРОНЕ СЕРВИСА осознанно: у
        коллектора ``uvicorn.access`` приглушён потому, что объём access-лога
        сопоставим с самим потоком событий, и перенос этого решения в библиотеку
        молча заглушил бы access-лог Movies API, где он нужен.
    """
    formatter_options: dict = {'()': JsonFormatter, 'include_request_id': include_request_id}
    if static_fields:
        formatter_options['static_fields'] = dict(static_fields)

    config: dict = {
        'version': 1,
        'disable_existing_loggers': False,
        'formatters': {
            'json': formatter_options,
            'verbose': {'format': VERBOSE_FORMAT_WITH_REQUEST_ID if include_request_id else VERBOSE_FORMAT},
        },
        'handlers': {
            'console': {
                'class': 'logging.StreamHandler',
                'formatter': 'json' if json_output else 'verbose',
                'stream': sys.stdout,
            },
        },
        'loggers': {
            name: {'handlers': ['console'], 'level': value if value is not None else level, 'propagate': False}
            for name, value in (logger_levels or {}).items()
        },
        'root': {'handlers': ['console'], 'level': level},
    }

    if include_request_id:
        # Фильтр нужен и JSON-, и текстовому формату: verbose ссылается на
        # %(request_id)s, и без фильтра форматирование упало бы на KeyError.
        config['filters'] = {'request_id': {'()': RequestIdFilter}}
        config['handlers']['console']['filters'] = ['request_id']

    return config


def level_from_env(default: str = 'INFO') -> str:
    """``LOG_LEVEL`` из окружения."""
    return os.environ.get('LOG_LEVEL', default).upper()


def json_output_from_env(default: bool = True) -> bool:
    """``LOG_JSON`` из окружения.

    Разбор именно такой (``not in ('0', 'false', 'no')``), а не ``bool(value)``:
    строка ``'False'`` истинна как объект Python, и наивная версия включала бы
    JSON там, где его явно выключили.
    """
    raw = os.environ.get('LOG_JSON', str(default))
    return raw.strip().lower() not in ('0', 'false', 'no')


def build_logging_config_from_env(*, logger_levels: Mapping[str, str] | None = None) -> dict:
    """``build_logging_config``, читающий уровень и формат из окружения.

    Для ``rest`` и ``auth``: их ``core/config.py`` вызывает ``setup_logging()`` НА
    ИМПОРТЕ, поэтому логгер не имеет права импортировать настройки — иначе цикл.
    Обе пары строк, читавших окружение, были идентичны, и разъехаться они могли
    только в одну сторону: кто-то поправил бы разбор ``LOG_JSON`` в одном сервисе.
    """
    return build_logging_config(
        level=level_from_env(),
        json_output=json_output_from_env(),
        logger_levels=logger_levels,
    )


def setup_logging(config: dict) -> None:
    """Применяет конфигурацию. Тонкая обёртка — чтобы сервисы не импортировали
    ``logging.config`` каждый по отдельности."""
    logging_config.dictConfig(config)
