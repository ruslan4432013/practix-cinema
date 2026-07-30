"""Настройка логирования Auth-сервиса.

До появления этого модуля логирования в сервисе не было вовсе: ни
``dictConfig``, ни форматтера — только ``logging.getLogger(__name__)`` в модуле
трассировки. Практическое следствие было конкретным: ``x-request-id`` доходил
до лога Nginx и до тега спана в Jaeger, но найти по нему строки логов Auth было
нельзя.

Формат — JSON с ``request_id`` в каждой записи. Устройство совпадает с
``rest/core/logger.py`` и ``analytics_collector/src/core/logger.py``;
дублирование осознанное — сервисы деплоятся независимо.
"""

import json
import logging
import os
import sys
from logging import config as logging_config

from core.request_id import request_id_ctx

LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
LOG_JSON = os.environ.get('LOG_JSON', 'True').strip().lower() not in ('0', 'false', 'no')

_RESERVED_RECORD_FIELDS = frozenset(
    {
        'args', 'asctime', 'created', 'exc_info', 'exc_text', 'filename', 'funcName',
        'levelname', 'levelno', 'lineno', 'module', 'msecs', 'message', 'msg', 'name',
        'pathname', 'process', 'processName', 'relativeCreated', 'stack_info',
        'thread', 'threadName', 'taskName',
    }
)  # fmt: skip


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
        for key, value in record.__dict__.items():
            if key not in _RESERVED_RECORD_FIELDS and key not in payload:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - [rid=%(request_id)s] - %(message)s'

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'filters': {
        'request_id': {'()': RequestIdFilter},
    },
    'formatters': {
        'json': {'()': JsonFormatter},
        'verbose': {'format': LOG_FORMAT},
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'json' if LOG_JSON else 'verbose',
            'filters': ['request_id'],
            'stream': sys.stdout,
        },
    },
    'loggers': {
        'uvicorn': {'handlers': ['console'], 'level': LOG_LEVEL, 'propagate': False},
        'uvicorn.error': {'handlers': ['console'], 'level': LOG_LEVEL, 'propagate': False},
        'uvicorn.access': {'handlers': ['console'], 'level': LOG_LEVEL, 'propagate': False},
        # SQLAlchemy на INFO печатает каждый SQL-запрос — на боевом трафике это
        # тот же объём, что и сам трафик.
        'sqlalchemy.engine': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
    },
    'root': {'handlers': ['console'], 'level': LOG_LEVEL},
}


def setup_logging() -> None:
    logging_config.dictConfig(LOGGING)
