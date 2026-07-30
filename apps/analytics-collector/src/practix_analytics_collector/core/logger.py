"""Настройка логирования коллектора.

В каждую запись подмешивается ``request_id``. Без него разбор инцидента в
сервисе, который обрабатывает тысячи запросов в секунду, сводится к угадыванию —
а идентификатор уже есть в логе Nginx и в теге спана Jaeger, так что связав их,
можно пройти путь запроса целиком.

Формат по умолчанию — JSON: логи собираются машиной, а не читаются глазами.
Для локальной разработки ``LOG_JSON=False`` возвращает обычный текст.

Реализация переехала в ``practix_core.logging``. В отличие от ``rest``/``auth``
здесь значения берутся из ``settings``, а не из окружения: у коллектора нет
цикла «настройки импортируют логгер» — ``core/config.py`` логгер не вызывает.
Библиотека одинаково устраивает оба случая именно потому, что сама ничего о
настройках не знает.
"""

from practix_analytics_collector.core.config import settings
from practix_core.logging import build_logging_config
from practix_core.logging import setup_logging as _setup_logging


def build_config() -> dict:
    return build_logging_config(
        level=settings.LOG_LEVEL,
        json_output=settings.LOG_JSON,
        # Имя сервиса в каждой записи — то же OTEL_SERVICE_NAME, что у
        # трассировки. В общем хранилище логов записи иначе различимы только по
        # имени контейнера, то есть по метаданным доставки, а не по самой строке.
        static_fields={'service': settings.OTEL_SERVICE_NAME},
        logger_levels={
            'uvicorn': settings.LOG_LEVEL,
            'uvicorn.error': settings.LOG_LEVEL,
            # Access-лог приглушён НАМЕРЕННО: ingest-ручка вызывается на каждое
            # действие пользователя, и построчный access-лог по объёму
            # сопоставим с самим потоком событий. Учёт запросов ведётся
            # метриками, а полная картина запроса — в Nginx и Jaeger.
            # Значение живёт здесь, а не в библиотеке: перенеси его туда — и
            # access-лог Movies API, где он нужен, тихо замолчал бы.
            'uvicorn.access': 'WARNING',
            # aiokafka на INFO пишет строку на каждую смену координатора и на
            # каждый ребаланс.
            'aiokafka': 'WARNING',
        },
    )


LOGGING = build_config()


def setup_logging() -> None:
    _setup_logging(LOGGING)
