"""Middleware для обязательного заголовка ``X-Request-Id`` — реэкспорт.

Реализация переехала в ``practix_core.request_id``: этот модуль и его копия в
``rest/core/request_id.py`` различались ТОЛЬКО комментариями.

Auth использует поведение по умолчанию (400 при отсутствии заголовка): его
ручки вызываются либо через Nginx, либо другими сервисами, поэтому жёсткое
требование оправдано.
"""

from practix_core.request_id import MAX_REQUEST_ID_LENGTH as MAX_REQUEST_ID_LENGTH
from practix_core.request_id import REQUEST_ID_HEADER as REQUEST_ID_HEADER
from practix_core.request_id import RequestIdMiddleware as RequestIdMiddleware
from practix_core.request_id import get_request_id as get_request_id
from practix_core.request_id import request_id_ctx as request_id_ctx
