"""Middleware сквозного идентификатора запроса ``X-Request-Id`` — реэкспорт.

Реализация живёт в ``practix_core.request_id``. Подключается в ``main.py`` с
``on_missing='generate'``, а НЕ с умолчанием ``reject_400`` как в UGC: маршрут
``/s/{code}`` открывает браузер по ссылке из письма, и заголовка там нет.
``reject_400`` превратил бы пользовательский редирект в 400, если запрос дошёл
минуя nginx (прямой порт контейнера, curl при отладке, проба контейнера). Аудит
от этого не страдает: nginx перетирает заголовок своим ``$request_id`` для всего
внешнего трафика. Ту же развилку прошёл сервис нотификаций.
"""

from practix_core.request_id import MAX_REQUEST_ID_LENGTH as MAX_REQUEST_ID_LENGTH
from practix_core.request_id import REQUEST_ID_HEADER as REQUEST_ID_HEADER
from practix_core.request_id import RequestIdMiddleware as RequestIdMiddleware
from practix_core.request_id import get_request_id as get_request_id
from practix_core.request_id import request_id_ctx as request_id_ctx
