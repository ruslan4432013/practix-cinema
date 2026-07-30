"""Ограничение размера тела запроса.

Ingest-ручка публичная и анонимная, поэтому размер полезной нагрузки нужно
ограничивать до того, как её начнёт разбирать Pydantic: иначе один запрос на
несколько десятков мегабайт занимает память воркера ещё до всякой валидации.

Проверка двухступенчатая, потому что ``Content-Length`` не является гарантией:

1. Если заголовок есть и он больше лимита — отказ сразу, тело даже не читается.
2. Тело в любом случае читается через счётчик: при ``Transfer-Encoding: chunked``
   заголовка ``Content-Length`` нет, а при подделанном значении реальный объём
   может оказаться больше заявленного.

Реализовано как чистый ASGI-middleware, а не ``BaseHTTPMiddleware``: нужен
доступ к потоку ``receive`` до того, как тело будет собрано целиком.
"""

import json

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Тело ответа формируем вручную: на этом уровне приложения FastAPI ещё нет.
_PAYLOAD_TOO_LARGE = json.dumps({'detail': 'Request body is too large'}).encode('utf-8')


class BodySizeLimitMiddleware:
    """Отклоняет запросы с телом больше ``max_body_bytes``."""

    def __init__(self, app: ASGIApp, max_body_bytes: int):
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        content_length = headers.get('content-length')
        if content_length is not None:
            try:
                if int(content_length) > self.max_body_bytes:
                    await self._reject(send)
                    return
            except ValueError:
                # Некорректный Content-Length — пусть с ним разбирается сервер,
                # наша задача только не пропустить заведомо большое тело.
                pass

        received = 0
        exceeded = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message['type'] == 'http.request':
                received += len(message.get('body', b''))
                if received > self.max_body_bytes:
                    exceeded = True
                    # Обрываем поток: приложение увидит пустое завершённое тело
                    # и вернёт ошибку валидации, а мы перекроем ответ ниже.
                    return {'type': 'http.disconnect'}
            return message

        response_started = False

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if exceeded:
                # Тело превысило лимит — ответ приложения подменяем на 413
                # (один раз), всё остальное, что оно попытается отправить,
                # отбрасываем.
                if not response_started:
                    response_started = True
                    await self._reject(send)
                return
            if message['type'] == 'http.response.start':
                response_started = True
            await send(message)

        await self.app(scope, limited_receive, guarded_send)

        # Получив http.disconnect, приложение может завершиться, вообще ничего
        # не отправив — тогда 413 отдаём здесь, иначе клиент останется без ответа.
        if exceeded and not response_started:
            response_started = True
            await self._reject(send)

    @staticmethod
    async def _reject(send: Send) -> None:
        await send(
            {
                'type': 'http.response.start',
                'status': 413,
                'headers': [
                    (b'content-type', b'application/json'),
                    (b'content-length', str(len(_PAYLOAD_TOO_LARGE)).encode('latin-1')),
                ],
            }
        )
        await send({'type': 'http.response.body', 'body': _PAYLOAD_TOO_LARGE})
