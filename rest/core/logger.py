"""Настройка логирования Movies API.

Формат — JSON, и в каждой записи есть ``request_id``. Без него сквозной
идентификатор доходил до лога Nginx и до тега спана в Jaeger, но найти по нему
строки логов самого приложения было нельзя — а при разборе инцидента это ровно
тот шаг, который нужен чаще всего.

Модуль намеренно НЕ импортирует ``core.config``: настройки сами импортируют
логгер (``core/config.py`` вызывает ``dictConfig`` на импорте), и обратная
зависимость замкнула бы цикл. Поэтому уровень и формат читаются прямо из
окружения.

Тот же по устройству модуль есть в ``auth/src/core/logger.py`` и
``analytics_collector/src/core/logger.py``. Дублирование осознанное: сервисы
деплоятся независимо и не должны тянуть общий пакет ради тридцати строк.
"""

import json
import logging
import os
import sys

from core.request_id import request_id_ctx

LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
# JSON-логи собирает машина (ELK/Loki); в локальной разработке читаемее текст.
LOG_JSON = os.environ.get('LOG_JSON', 'True').strip().lower() not in ('0', 'false', 'no')

# Поля LogRecord, которые не являются пользовательскими и в JSON не нужны.
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
        # Всё, что передали через extra=..., попадает в JSON как есть.
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
        # Единый обработчик у всех логгеров uvicorn: иначе access-лог шёл бы
        # своим форматом и остался бы без request_id.
        'uvicorn': {'handlers': ['console'], 'level': LOG_LEVEL, 'propagate': False},
        'uvicorn.error': {'handlers': ['console'], 'level': LOG_LEVEL, 'propagate': False},
        'uvicorn.access': {'handlers': ['console'], 'level': LOG_LEVEL, 'propagate': False},
        'elastic_transport': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
    },
    'root': {'handlers': ['console'], 'level': LOG_LEVEL},
}


def setup_logging() -> None:
    from logging import config as logging_config

    logging_config.dictConfig(LOGGING)
