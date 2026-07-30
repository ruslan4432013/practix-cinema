"""JSON-формат логов админки — форк ``practix_core.logging.JsonFormatter``.

ПОЧЕМУ ФОРК. Та же граница, из-за которой форкнуты RequestId-middleware в
``example/middleware.py`` и инициализация сбора ошибок в ``example/wsgi.py``:
этот проект вне uv-workspace (Python 3.12, собственный ``uv.lock``), собирается
из контекста ``apps/django-admin`` и каталога ``libs/`` не видит вовсе, а сама
библиотека объявляет ``requires-python >= 3.13``. Подключить её означало бы
опустить планку версии библиотеке, перенести контекст сборки в корень
репозитория, переписать все ``COPY`` в её Dockerfile и снять инвариант «у
django-admin рёбер в графе проектов нет и быть не должно»
(``tools/check_graph_edges.py``). Это структурное решение, а не решение про логи.

НАБОР И ПОРЯДОК КЛЮЧЕЙ совпадают с библиотечными НАМЕРЕННО: в Kibana это один
индекс и один набор полей, и запрос ``service: django-admin and level: ERROR``
обязан работать так же, как для остальных сервисов. Порядок закреплён тестом
``test_service_field_follows_request_id_in_key_order`` на стороне библиотеки.

``request_id`` здесь НЕТ: контекстную переменную заполняет middleware
библиотеки, которой тут тоже нет. Корреляция запроса идёт через access-лог
nginx, где ``request_id`` есть у каждой строки.
"""

import json
import logging


class JsonFormatter(logging.Formatter):
    """Одна строка JSON на запись; поле ``service`` задаётся конфигурацией."""

    def __init__(self, *args, service: str = 'django-admin', **kwargs):
        super().__init__(*args, **kwargs)
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            'timestamp': self.formatTime(record, '%Y-%m-%dT%H:%M:%S%z'),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'service': self.service,
        }
        if record.exc_info:
            payload['exception'] = self.formatException(record.exc_info)
        # ensure_ascii=False: иначе русские сообщения превращаются в \uXXXX.
        # default=str: неожиданный объект в записи не должен ронять сам лог.
        return json.dumps(payload, ensure_ascii=False, default=str)
