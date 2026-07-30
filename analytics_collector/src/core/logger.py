"""Настройка логирования.

Отличие от ``rest/core/logger.py``: в каждую запись подмешивается
``request_id``. Без него разбор инцидента в сервисе, который обрабатывает
тысячи запросов в секунду, сводится к угадыванию — а идентификатор уже есть в
логе Nginx и в теге спана Jaeger, так что связав их, можно пройти путь запроса
целиком.

Формат по умолчанию — JSON: логи собираются машиной, а не читаются глазами.
Для локальной разработки ``LOG_JSON=False`` возвращает обычный текст.
"""

import json
import logging
import sys
from logging import config as logging_config

from core.config import settings
from core.request_id import request_id_ctx

# Поля LogRecord, которые не являются пользовательскими и в JSON не нужны.
_RESERVED_RECORD_FIELDS = frozenset(
    {
        'args',
        'asctime',
        'created',
        'exc_info',
        'exc_text',
        'filename',
        'funcName',
        'levelname',
        'levelno',
        'lineno',
        'module',
        'msecs',
        'message',
        'msg',
        'name',
        'pathname',
        'process',
        'processName',
        'relativeCreated',
        'stack_info',
        'thread',
        'threadName',
        'taskName',
    }
)


class RequestIdFilter(logging.Filter):
    """Добавляет в запись идентификатор текущего запроса."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get() or '-'
        return True


class JsonFormatter(logging.Formatter):
    """Форматирует запись в одну строку JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            'timestamp': self.formatTime(record, '%Y-%m-%dT%H:%M:%S%z'),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'request_id': getattr(record, 'request_id', '-'),
        }
        if record.exc_info:
            payload['exception'] = self.formatException(record.exc_info)
        # Всё, что передали через extra=..., попадает в JSON как есть.
        for key, value in record.__dict__.items():
            if key not in _RESERVED_RECORD_FIELDS and key not in payload:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def build_logging_config() -> dict:
    formatter = 'json' if settings.LOG_JSON else 'verbose'
    return {
        'version': 1,
        'disable_existing_loggers': False,
        'filters': {
            'request_id': {'()': RequestIdFilter},
        },
        'formatters': {
            'json': {'()': JsonFormatter},
            'verbose': {
                'format': '%(asctime)s - %(name)s - %(levelname)s - [rid=%(request_id)s] - %(message)s',
            },
        },
        'handlers': {
            'console': {
                'class': 'logging.StreamHandler',
                'formatter': formatter,
                'filters': ['request_id'],
                'stream': sys.stdout,
            },
        },
        'loggers': {
            'uvicorn': {'handlers': ['console'], 'level': settings.LOG_LEVEL, 'propagate': False},
            'uvicorn.error': {'handlers': ['console'], 'level': settings.LOG_LEVEL, 'propagate': False},
            # Access-лог выключен намеренно: ingest-ручка вызывается на каждое
            # действие пользователя, и построчный access-лог по объёму
            # сопоставим с самим потоком событий. Учёт запросов ведётся
            # метриками, а полная картина запроса — в Nginx и Jaeger.
            'uvicorn.access': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
            'aiokafka': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        },
        'root': {'handlers': ['console'], 'level': settings.LOG_LEVEL},
    }


LOGGING = build_logging_config()


def setup_logging() -> None:
    logging_config.dictConfig(LOGGING)
