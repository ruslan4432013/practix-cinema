"""Middleware сквозного идентификатора запроса ``X-Request-Id`` — реэкспорт.

Реализация переехала в ``practix_core.request_id``.

ОТЛИЧИЕ ОТ СИБЛИНГОВ СОХРАНЕНО, но выражено параметрами, а не отдельной копией
файла. В ``rest/`` и ``auth/`` отсутствие заголовка приводит к 400 — там все
вызовы идут либо через Nginx, либо между сервисами. Здесь ручка публичная и
вызывается из браузера, в том числе через ``navigator.sendBeacon``, который **не
умеет ставить произвольные заголовки**: требование заголовка означало бы, что
beacon-события (основной способ отправить «время на странице» при уходе с неё)
никогда не доедут.

Поэтому коллектор подключает middleware как
``add_middleware(RequestIdMiddleware, on_missing='generate', sanitize=True)`` —
см. ``main.py``. Отличие теперь видно в точке подключения, а не спрятано в
разошедшейся копии модуля.
"""

from practix_core.request_id import MAX_REQUEST_ID_LENGTH as MAX_REQUEST_ID_LENGTH
from practix_core.request_id import REQUEST_ID_HEADER as REQUEST_ID_HEADER
from practix_core.request_id import RequestIdMiddleware as RequestIdMiddleware
from practix_core.request_id import get_request_id as get_request_id
from practix_core.request_id import request_id_ctx as request_id_ctx
