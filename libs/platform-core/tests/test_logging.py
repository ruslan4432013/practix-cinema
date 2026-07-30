"""Проверки, что извлечённый логгер выдаёт ПОБАЙТОВО те же строки, что и раньше.

Логгер был самым крупным дублем в репозитории (четыре копии, pylint показывал
71 схожую строку между `rest` и `auth`), и одновременно самым рискованным для
извлечения: ``core/config.py`` вызывает ``dictConfig`` на импорте. Поэтому
основной тест здесь — сравнение выданной строки с эталоном, полученным от
ДОСЛОВНОЙ копии прежней реализации, а не проверка «логгер что-то пишет».
"""

import json
import logging
import sys

import pytest

from practix_core.logging import (
    RESERVED_RECORD_FIELDS,
    JsonFormatter,
    RequestIdFilter,
    build_logging_config,
    setup_logging,
)

# --- Эталон: дословная копия прежнего кода из rest/core/logger.py ------------

_LEGACY_RESERVED = frozenset(
    {
        'args', 'asctime', 'created', 'exc_info', 'exc_text', 'filename', 'funcName',
        'levelname', 'levelno', 'lineno', 'module', 'msecs', 'message', 'msg', 'name',
        'pathname', 'process', 'processName', 'relativeCreated', 'stack_info',
        'thread', 'threadName', 'taskName',
    }
)  # fmt: skip


class _LegacyRestFormatter(logging.Formatter):
    """Прежний JsonFormatter из rest/auth/analytics_collector."""

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
            if key not in _LEGACY_RESERVED and key not in payload:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


class _LegacyEtlFormatter(logging.Formatter):
    """Прежний JsonFormatter из etl_clickhouse: 'service' вместо 'request_id'."""

    def __init__(self, service_name: str):
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            'timestamp': self.formatTime(record, '%Y-%m-%dT%H:%M:%S%z'),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'service': self.service_name,
        }
        if record.exc_info:
            payload['exception'] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key not in _LEGACY_RESERVED and key not in payload:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def _record(**extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name='practix.test',
        level=logging.WARNING,
        pathname=__file__,
        lineno=42,
        msg='событие %s',
        args=('произошло',),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


# --- Эквивалентность вывода -------------------------------------------------


def test_reserved_fields_match_legacy_set_exactly():
    assert RESERVED_RECORD_FIELDS == _LEGACY_RESERVED


def test_json_line_is_identical_to_legacy_rest_formatter():
    record = _record(request_id='rid-1')
    assert JsonFormatter().format(record) == _LegacyRestFormatter().format(record)


def test_json_line_identical_to_legacy_when_request_id_absent():
    """Прежний код подставлял '-' — поведение должно совпадать."""
    record = _record()
    new = JsonFormatter().format(record)
    assert new == _LegacyRestFormatter().format(record)
    assert json.loads(new)['request_id'] == '-'


def test_json_line_identical_to_legacy_etl_formatter():
    record = _record()
    new = JsonFormatter(include_request_id=False, static_fields={'service': 'etl-clickhouse'}).format(record)
    assert new == _LegacyEtlFormatter('etl-clickhouse').format(record)
    payload = json.loads(new)
    assert payload['service'] == 'etl-clickhouse'
    # У фонового консьюмера поля request_id быть не должно вовсе.
    assert 'request_id' not in payload


def test_extra_fields_are_passed_through_like_legacy():
    record = _record(request_id='rid-2', user_id=7, topic='ugc.clicks.v1')
    new = JsonFormatter().format(record)
    assert new == _LegacyRestFormatter().format(record)
    payload = json.loads(new)
    assert payload['user_id'] == 7
    assert payload['topic'] == 'ugc.clicks.v1'


def test_exception_is_rendered_like_legacy():
    try:
        raise ValueError('сломалось')
    except ValueError:
        exc_info = sys.exc_info()
    record = _record(request_id='rid-3')
    record.exc_info = exc_info
    assert JsonFormatter().format(record) == _LegacyRestFormatter().format(record)
    assert 'ValueError' in json.loads(JsonFormatter().format(record))['exception']


def test_cyrillic_is_not_escaped():
    """ensure_ascii=False сохранено: иначе русские сообщения станут \\uXXXX."""
    payload = json.loads(JsonFormatter().format(_record(request_id='r')))
    assert payload['message'] == 'событие произошло'


def test_non_serialisable_extra_falls_back_to_str():
    """default=str сохранён: неожиданный объект в extra не должен ронять лог."""
    record = _record(request_id='r', obj=object())
    assert 'object object at' in json.loads(JsonFormatter().format(record))['obj']


# --- Структура конфигурации -------------------------------------------------


def test_config_wires_request_id_filter_when_enabled():
    config = build_logging_config(level='INFO', json_output=True)
    assert config['filters'] == {'request_id': {'()': RequestIdFilter}}
    assert config['handlers']['console']['filters'] == ['request_id']


def test_config_omits_filter_when_request_id_disabled():
    """У ETL фильтра быть не должно — как и в прежней его версии."""
    config = build_logging_config(level='INFO', json_output=True, include_request_id=False)
    assert 'filters' not in config
    assert 'filters' not in config['handlers']['console']


def test_verbose_format_includes_rid_only_when_request_id_enabled():
    with_rid = build_logging_config(include_request_id=True)['formatters']['verbose']['format']
    without = build_logging_config(include_request_id=False)['formatters']['verbose']['format']
    assert '[rid=%(request_id)s]' in with_rid
    assert 'request_id' not in without


def test_json_output_flag_selects_formatter():
    assert build_logging_config(json_output=True)['handlers']['console']['formatter'] == 'json'
    assert build_logging_config(json_output=False)['handlers']['console']['formatter'] == 'verbose'


def test_logger_levels_are_per_service():
    config = build_logging_config(logger_levels={'uvicorn.access': 'WARNING', 'aiokafka': 'WARNING'})
    assert config['loggers']['uvicorn.access'] == {
        'handlers': ['console'],
        'level': 'WARNING',
        'propagate': False,
    }
    assert config['loggers']['aiokafka']['level'] == 'WARNING'


def test_root_level_is_applied():
    assert build_logging_config(level='DEBUG')['root']['level'] == 'DEBUG'


# --- Реальное применение через dictConfig -----------------------------------


def test_verbose_config_applies_without_keyerror_on_request_id(capsys):
    """Текстовый формат ссылается на %(request_id)s: без фильтра был бы KeyError."""
    setup_logging(build_logging_config(level='INFO', json_output=False))
    logging.getLogger('practix.verbose').warning('привет')
    err = capsys.readouterr()
    assert 'rid=-' in (err.out + err.err)


def test_json_config_applies_and_emits_parsable_line(capsys):
    setup_logging(build_logging_config(level='INFO', json_output=True))
    logging.getLogger('practix.json').warning('строка')
    captured = capsys.readouterr()
    line = (captured.out + captured.err).strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload['message'] == 'строка'
    assert payload['level'] == 'WARNING'
    assert payload['request_id'] == '-'


# --- Чтение окружения (было продублировано в rest и auth) -------------------


def test_level_from_env(monkeypatch):
    from practix_core.logging import level_from_env

    monkeypatch.setenv('LOG_LEVEL', 'debug')
    assert level_from_env() == 'DEBUG'
    monkeypatch.delenv('LOG_LEVEL')
    assert level_from_env() == 'INFO'


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('0', False),
        ('false', False),
        ('False', False),
        ('no', False),
        ('NO', False),
        ('1', True),
        ('true', True),
        ('True', True),
        ('yes', True),
        (' True ', True),
    ],
)
def test_json_output_from_env_parsing(monkeypatch, raw, expected):
    """Строка 'False' истинна как объект Python — наивный bool() был бы неверен."""
    from practix_core.logging import json_output_from_env

    monkeypatch.setenv('LOG_JSON', raw)
    assert json_output_from_env() is expected


def test_json_output_defaults_to_true(monkeypatch):
    from practix_core.logging import json_output_from_env

    monkeypatch.delenv('LOG_JSON', raising=False)
    assert json_output_from_env() is True


def test_none_logger_level_inherits_root(monkeypatch):
    """rest/auth задают уровень uvicorn как «тот же, что у root»."""
    from practix_core.logging import build_logging_config_from_env

    monkeypatch.setenv('LOG_LEVEL', 'WARNING')
    config = build_logging_config_from_env(logger_levels={'uvicorn': None, 'sqlalchemy.engine': 'ERROR'})
    assert config['loggers']['uvicorn']['level'] == 'WARNING'
    assert config['loggers']['sqlalchemy.engine']['level'] == 'ERROR'
    assert config['root']['level'] == 'WARNING'
