"""Настройка логирования ETL PostgreSQL -> Elasticsearch.

До этого модуля сервис обходился ``logging.basicConfig`` внутри ``main()``:
текстовый формат, никакого поля ``service``, ничего из ``extra=...``. В общем
хранилище логов такие строки не отфильтровать по сервису и не разобрать на
поля — а цикл в ``main()`` гасит исключения через ``except Exception:
logger.exception(...)``, то есть строка лога и есть основной след аварии.

``include_request_id=False`` — как у ETL ClickHouse: у фонового процесса нет
HTTP-запроса, и поле было бы всегда ``'-'``.

Значения берутся из ``settings``, а не из окружения: цикла «настройки
импортируют логгер» здесь нет — ``settings.py`` логгер не вызывает.
"""

from practix_core.logging import build_logging_config
from practix_core.logging import setup_logging as _setup_logging
from practix_etl_elasticsearch.settings import settings


def build_config() -> dict:
    return build_logging_config(
        level=settings.LOG_LEVEL,
        json_output=settings.LOG_JSON,
        include_request_id=False,
        static_fields={'service': settings.OTEL_SERVICE_NAME},
        logger_levels={
            # elastic_transport на INFO пишет строку на КАЖДЫЙ bulk-запрос — то
            # есть по строке на пачку документов, и объём лога становится
            # сопоставим с объёмом самой загрузки.
            'elastic_transport': 'WARNING',
            'urllib3': 'WARNING',
        },
    )


LOGGING = build_config()


def setup_logging() -> None:
    _setup_logging(LOGGING)
