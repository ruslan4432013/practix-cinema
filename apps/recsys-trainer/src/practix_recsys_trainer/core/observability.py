"""Общая обвязка наблюдаемости для обеих точек входа батча.

У сервиса их две — ``main`` (цикл по расписанию) и ``cli`` (команды, которые
запускает человек), — и настраивать логирование им надо одинаково. Держать этот
блок дважды значило бы получить два расходящихся формата логов у одного сервиса,
причём разошлись бы они молча: команда, запущенная руками, писала бы не то, что
пишет контейнер, и в Kibana это выглядело бы двумя разными сервисами.

Отдельного ``core/logger.py`` в стиле UGC здесь нет: там модуль-обёртка почти
дословно повторяет такой же в соседних сервисах, а этот собирает конфиг под
СВОИ шумные логгеры и переиспользуется внутри одного пакета.
"""

from practix_core.logging import build_logging_config, setup_logging
from practix_recsys_trainer.core.config import settings


def configure_logging() -> dict:
    """Настраивает логирование и возвращает конфиг (он же нужен uvicorn-подобным вызовам)."""
    config = build_logging_config(
        level=settings.LOG_LEVEL,
        json_output=settings.LOG_JSON,
        # Имя сервиса в каждой записи — то же OTEL_SERVICE_NAME, что у
        # трассировки: логи, трейсы и ошибки должны называть сервис одинаково.
        static_fields={'service': settings.OTEL_SERVICE_NAME},
        logger_levels={
            # SQLAlchemy на INFO печатает каждый запрос и его параметры, а батч
            # делает их тысячами: витрина пишется пачками по десять тысяч строк.
            'sqlalchemy.engine': 'WARNING',
            # clickhouse-connect на INFO логирует каждую вставку целиком.
            'clickhouse_connect': 'WARNING',
        },
    )
    setup_logging(config)
    return config
