"""Настройка логирования Movies API.

Формат — JSON, и в каждой записи есть ``request_id``. Без него сквозной
идентификатор доходил до лога Nginx и до тега спана в Jaeger, но найти по нему
строки логов самого приложения было нельзя — а при разборе инцидента это ровно
тот шаг, который нужен чаще всего.

Реализация в ``practix_core.logging``: модуль существовал в четырёх почти
одинаковых копиях (pylint показывал 71 схожую строку между этим файлом и
``auth/src/core/logger.py``).

Уровень и формат читаются из ОКРУЖЕНИЯ, а не из ``core.config``: настройки сами
импортируют логгер (``core/config.py`` вызывает ``setup_logging()`` на импорте), и
обратная зависимость замкнула бы цикл. Библиотека тоже не знает о настройках —
это её инвариант, а не совпадение.
"""

from practix_core.logging import build_logging_config_from_env
from practix_core.logging import setup_logging as _setup_logging

LOGGING = build_logging_config_from_env(
    logger_levels={
        # Единый обработчик у всех логгеров uvicorn: иначе access-лог шёл бы
        # своим форматом и остался бы без request_id. Уровень берётся из
        # окружения — тот же LOG_LEVEL, что и у root.
        'uvicorn': None,
        'uvicorn.error': None,
        'uvicorn.access': None,
        'elastic_transport': 'WARNING',
    },
)


def setup_logging() -> None:
    _setup_logging(LOGGING)
