from elasticsearch import AsyncElasticsearch, NotFoundError

from db.base import AsyncDataStorage

es: AsyncElasticsearch | None = None


class ElasticsearchStorage(AsyncDataStorage):
    def __init__(self, elastic: AsyncElasticsearch):
        self.elastic = elastic

    async def get_by_id(self, index: str, entity_id: str) -> dict | None:
        try:
            doc = await self.elastic.get(index=index, id=entity_id)
        except NotFoundError:
            return None
        return doc['_source']

    async def search(self, index: str, body: dict) -> dict:
        return await self.elastic.search(index=index, body=body)


# Функция понадобится при внедрении зависимостей
def get_elastic() -> AsyncElasticsearch:
    return es
