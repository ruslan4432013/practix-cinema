import uuid
from dataclasses import dataclass
from typing import Any

import aiohttp
import pytest
import pytest_asyncio

from practix_testing.settings import test_settings


@dataclass
class HTTPResponse:
    body: Any
    headers: dict
    status: int


@pytest_asyncio.fixture(scope='session')
async def http_session():
    session = aiohttp.ClientSession()
    yield session
    await session.close()


@pytest.fixture
def make_get_request(http_session: aiohttp.ClientSession):
    async def inner(
        path: str,
        params: dict | None = None,
        headers: dict | None = None,
    ) -> HTTPResponse:
        # X-Request-Id обязателен для КАЖДОГО запроса к Movies API: без него
        # RequestIdMiddleware отвечает 400 ещё до маршрутизации, и весь набор
        # падал на `assert 400 == 200`. В проде заголовок ставит Nginx, а
        # тесты ходят в сервис напрямую, минуя его, — как и любой другой
        # внутрисетевой клиент, который обязан проставлять заголовок сам.
        # Значение уникально на запрос: так в логах сервиса видно, какой
        # именно тест их породил.
        request_headers = {'X-Request-Id': str(uuid.uuid4())}
        if headers:
            request_headers.update(headers)

        url = test_settings.service_url + path
        async with http_session.get(url, params=params, headers=request_headers) as response:
            try:
                body = await response.json()
            except aiohttp.ContentTypeError:
                body = await response.text()
            return HTTPResponse(
                body=body,
                headers=dict(response.headers),
                status=response.status,
            )

    return inner
