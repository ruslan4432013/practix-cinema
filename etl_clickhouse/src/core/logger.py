"""Настройка логирования.

Повторяет ``analytics_collector/src/core/logger.py`` — JSON одной строкой,
всё из ``extra=...`` попадает в запись как есть, — но без фильтра request_id:
у фонового консьюмера нет HTTP-запроса, а идентификатор запроса, с которым
событие когда-то пришло в коллектор, доступен в заголовке сообщения и
логируется точечно, при разборе отдельных инцидентов.

Старый ``etl/`` обходится ``logging.basicConfig``; здесь берётся более поздний
подход коллектора: сервис работает непрерывно, и его логи будут читать
машиной, а не глазами.
"""

import json
import logging
import sys
from logging import config as logging_config

from core.config import settings

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


class JsonFormatter(logging.Formatter):
    """Форматирует запись в одну строку JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            'timestamp': self.formatTime(record, '%Y-%m-%dT%H:%M:%S%z'),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'service': settings.PROJECT_NAME,
        }
        if record.exc_info:
            payload['exception'] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key not in _RESERVED_RECORD_FIELDS and key not in payload:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def build_logging_config() -> dict:
    formatter = 'json' if settings.LOG_JSON else 'verbose'
    return {
        'version': 1,
        'disable_existing_loggers': False,
        'formatters': {
            'json': {'()': JsonFormatter},
            'verbose': {'format': '%(asctime)s - %(name)s - %(levelname)s - %(message)s'},
        },
        'handlers': {
            'console': {
                'class': 'logging.StreamHandler',
                'formatter': formatter,
                'stream': sys.stdout,
            },
        },
        'loggers': {
            # aiokafka на INFO пишет строку на каждую смену координатора и на
            # каждый ребаланс — в норме это шум, а в аварии всё равно видно по
            # нашим собственным WARNING'ам.
            'aiokafka': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
            # clickhouse-connect на DEBUG логирует тело каждого запроса, то есть
            # весь поток событий целиком.
            'clickhouse_connect': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
            'urllib3': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        },
        'root': {'handlers': ['console'], 'level': settings.LOG_LEVEL},
    }


LOGGING = build_logging_config()


def setup_logging() -> None:
    logging_config.dictConfig(LOGGING)
