"""Настройка логирования Auth-сервиса.

До появления этого модуля логирования в сервисе не было вовсе: ни
``dictConfig``, ни форматтера — только ``logging.getLogger(__name__)`` в модуле
трассировки. Практическое следствие было конкретным: ``x-request-id`` доходил до
лога Nginx и до тега спана в Jaeger, но найти по нему строки логов Auth было
нельзя.

Реализация в ``practix_core.logging``; с ``rest/core/logger.py`` этот модуль
расходился только комментариями. Уровень и формат читаются из окружения, а не из
``core.config``: настройки сами импортируют логгер, и обратная зависимость
замкнула бы цикл.
"""

from practix_core.logging import build_logging_config_from_env
from practix_core.logging import setup_logging as _setup_logging

LOGGING = build_logging_config_from_env(
    logger_levels={
        # None — тот же уровень, что у root (из LOG_LEVEL).
        'uvicorn': None,
        'uvicorn.error': None,
        'uvicorn.access': None,
        # SQLAlchemy на INFO печатает каждый SQL-запрос — на боевом трафике это
        # тот же объём, что и сам трафик.
        'sqlalchemy.engine': 'WARNING',
    },
)


def setup_logging() -> None:
    _setup_logging(LOGGING)
