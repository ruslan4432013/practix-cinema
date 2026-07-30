"""Middleware для обязательного заголовка ``X-Request-Id`` — реэкспорт.

Реализация переехала в ``practix_core.request_id``: этот модуль и его копия в
``auth/src/core/request_id.py`` различались ТОЛЬКО комментариями, ни одной
отличающейся строки логики в них не было.

Movies API использует поведение по умолчанию: каждый входящий запрос обязан
содержать ``X-Request-Id`` (его проставляет Nginx), запрос без него отклоняется
с 400 — это гарантирует, что любой сбой можно проаудировать по единому
идентификатору.
"""

from practix_core.request_id import MAX_REQUEST_ID_LENGTH as MAX_REQUEST_ID_LENGTH
from practix_core.request_id import REQUEST_ID_HEADER as REQUEST_ID_HEADER
from practix_core.request_id import RequestIdMiddleware as RequestIdMiddleware
from practix_core.request_id import get_request_id as get_request_id
from practix_core.request_id import request_id_ctx as request_id_ctx
