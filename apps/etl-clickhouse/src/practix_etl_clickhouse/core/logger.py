"""Настройка логирования ETL ClickHouse.

JSON одной строкой, всё из ``extra=...`` попадает в запись как есть — но БЕЗ
поля ``request_id``: у фонового консьюмера нет HTTP-запроса, и поле было бы
всегда ``'-'``. Идентификатор запроса, с которым событие когда-то пришло в
коллектор, доступен в заголовке сообщения и логируется точечно, при разборе
отдельных инцидентов.

Вместо ``request_id`` в каждую запись подмешивается ``service``: в общем
хранилище логов записи фонового процесса иначе неотличимы друг от друга.

Реализация переехала в ``practix_core.logging``; отличия от сиблингов выражены
аргументами (``include_request_id=False`` и ``static_fields``), а не отдельной
копией модуля. Старый ``etl/`` обходится ``logging.basicConfig``.
"""

from practix_core.logging import build_logging_config
from practix_core.logging import setup_logging as _setup_logging
from practix_etl_clickhouse.core.config import settings


def build_config() -> dict:
    return build_logging_config(
        level=settings.LOG_LEVEL,
        json_output=settings.LOG_JSON,
        include_request_id=False,
        # OTEL_SERVICE_NAME, а не PROJECT_NAME. Поле `service` существует ровно
        # для того, чтобы различать сервисы в общем хранилище логов, — и до этой
        # правки оно врало: PROJECT_NAME объявлен в корневом .env как `movies`
        # (общая переменная всего стенда) и перекрывал верный дефолт
        # 'etl-clickhouse' из настроек. В логах ETL стояло "service": "movies".
        #
        # OTEL_SERVICE_NAME задаётся отдельно каждому сервису в compose, то есть
        # уже является каноническим именем, и логи с трассировкой теперь называют
        # сервис одинаково — иначе связывать их пришлось бы вручную.
        static_fields={'service': settings.OTEL_SERVICE_NAME},
        logger_levels={
            # aiokafka на INFO пишет строку на каждую смену координатора и на
            # каждый ребаланс — в норме это шум, а в аварии всё равно видно по
            # нашим собственным WARNING'ам.
            'aiokafka': 'WARNING',
            # clickhouse-connect на DEBUG логирует тело каждого запроса, то есть
            # весь поток событий целиком.
            'clickhouse_connect': 'WARNING',
            'urllib3': 'WARNING',
        },
    )


LOGGING = build_config()


def setup_logging() -> None:
    _setup_logging(LOGGING)
