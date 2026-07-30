"""Middleware сквозного идентификатора запроса ``X-Request-Id`` — реэкспорт.

Реализация живёт в ``practix_core.request_id``. Поведение по умолчанию
(``on_missing='reject_400'``) здесь и нужно: сервис вызывается через Nginx,
который проставляет заголовок сам. Генерация идентификатора, как у коллектора,
существует только ради ``navigator.sendBeacon`` — здесь такого пути нет.
"""

from practix_core.request_id import MAX_REQUEST_ID_LENGTH as MAX_REQUEST_ID_LENGTH
from practix_core.request_id import REQUEST_ID_HEADER as REQUEST_ID_HEADER
from practix_core.request_id import RequestIdMiddleware as RequestIdMiddleware
from practix_core.request_id import get_request_id as get_request_id
from practix_core.request_id import request_id_ctx as request_id_ctx
