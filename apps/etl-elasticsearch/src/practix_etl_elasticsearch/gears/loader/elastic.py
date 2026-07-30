import logging
from typing import Any

from elasticsearch import ConnectionError as ESConnectionError
from elasticsearch import Elasticsearch, TransportError
from elasticsearch.helpers import bulk

from practix_etl_elasticsearch.lib.backoff import backoff

logger = logging.getLogger(__name__)


class ElasticsearchLoader:
    """Загружает документы в Elasticsearch пачками через bulk API."""

    def __init__(self, host: str, port: int, index_name: str, index_body: dict | None = None) -> None:
        self.host = host
        self.port = port
        self.index_name = index_name
        self.index_body = index_body
        self._client: Elasticsearch | None = None

    @backoff(exceptions=(ESConnectionError, TransportError))
    def _get_client(self) -> Elasticsearch:
        if self._client is None:
            url = f'http://{self.host}:{self.port}'
            logger.info('Подключаемся к Elasticsearch %s', url)
            self._client = Elasticsearch(url, request_timeout=10)
            # Проверка живости — backoff повторит при ошибке.
            if not self._client.ping():
                self._client = None
                raise ESConnectionError('ES ping failed')
        return self._client

    @backoff(exceptions=(ESConnectionError, TransportError))
    def ensure_index(self) -> None:
        client = self._get_client()
        if not client.indices.exists(index=self.index_name):
            logger.info('Создаём индекс %s', self.index_name)
            client.indices.create(index=self.index_name, body=self.index_body)

    @backoff(exceptions=(ESConnectionError, TransportError))
    def load(self, docs: list[dict[str, Any]]) -> None:
        if not docs:
            return
        client = self._get_client()
        actions = (
            {
                '_op_type': 'index',
                '_index': self.index_name,
                '_id': doc['id'],
                '_source': doc,
            }
            for doc in docs
        )
        success, errors = bulk(client, actions, stats_only=False, raise_on_error=False)
        logger.info('Загружено в ES: %s, ошибок: %s', success, len(errors) if errors else 0)
        if errors:
            logger.error('Ошибки bulk: %s', errors[:3])
            raise TransportError('Bulk load errors')
