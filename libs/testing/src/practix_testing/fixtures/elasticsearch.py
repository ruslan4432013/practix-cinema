import contextlib

import pytest
import pytest_asyncio
from elasticsearch import AsyncElasticsearch, NotFoundError
from elasticsearch.helpers import async_bulk

from practix_search_schema import GENRES_INDEX, MOVIES_INDEX, PERSONS_INDEX
from practix_testing.settings import test_settings
from practix_testing.utils.helpers import get_es_bulk_query


@pytest_asyncio.fixture(scope='session')
async def es_client():
    client = AsyncElasticsearch(hosts=test_settings.es_host, verify_certs=False)
    yield client
    await client.close()


@pytest_asyncio.fixture
async def es_movies_index(es_client: AsyncElasticsearch):
    """Создаёт и удаляет индекс movies для теста."""
    index = test_settings.es_movies_index
    with contextlib.suppress(NotFoundError):
        await es_client.indices.delete(index=index)
    await es_client.indices.create(
        index=index,
        settings=MOVIES_INDEX['settings'],
        mappings=MOVIES_INDEX['mappings'],
    )
    yield index
    with contextlib.suppress(NotFoundError):
        await es_client.indices.delete(index=index)


@pytest_asyncio.fixture
async def es_persons_index(es_client: AsyncElasticsearch):
    """Создаёт и удаляет индекс persons для теста."""
    index = test_settings.es_persons_index
    with contextlib.suppress(NotFoundError):
        await es_client.indices.delete(index=index)
    await es_client.indices.create(
        index=index,
        settings=PERSONS_INDEX['settings'],
        mappings=PERSONS_INDEX['mappings'],
    )
    yield index
    with contextlib.suppress(NotFoundError):
        await es_client.indices.delete(index=index)


@pytest_asyncio.fixture
async def es_genres_index(es_client: AsyncElasticsearch):
    """Создаёт и удаляет индекс genres для теста."""
    index = test_settings.es_genres_index
    with contextlib.suppress(NotFoundError):
        await es_client.indices.delete(index=index)
    await es_client.indices.create(
        index=index,
        settings=GENRES_INDEX['settings'],
        mappings=GENRES_INDEX['mappings'],
    )
    yield index
    with contextlib.suppress(NotFoundError):
        await es_client.indices.delete(index=index)


@pytest.fixture
def es_write_data(es_client: AsyncElasticsearch):
    async def inner(docs: list[dict], index: str):
        actions = get_es_bulk_query(docs, index)
        prepared = []
        for i in range(0, len(actions), 2):
            meta = actions[i]['index']
            prepared.append(
                {
                    '_index': meta['_index'],
                    '_id': meta['_id'],
                    '_source': actions[i + 1],
                }
            )
        await async_bulk(es_client, prepared, refresh='wait_for')

    return inner
