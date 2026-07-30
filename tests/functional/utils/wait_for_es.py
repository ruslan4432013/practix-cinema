"""Ожидание готовности Elasticsearch перед запуском тестов."""

import logging
import os
import sys

from elasticsearch import Elasticsearch

# Каталог скрипта в sys.path: файл запускается напрямую (`python3 wait_for_es.py`),
# и относительный импорт соседнего модуля иначе не разрешается.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from retry_utils import backoff

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# max_attempts обязателен: без него недоступный ES вешал раннер навсегда, и в CI
# это выглядело бы как зависший job, а не как красный тест.
@backoff(max_attempts=30)
def wait_for_es(es_client: Elasticsearch):
    if not es_client.ping():
        raise ConnectionError('Elasticsearch is not available')


if __name__ == '__main__':
    host = os.getenv('ES_HOST', 'http://127.0.0.1:9200')
    wait_for_es(Elasticsearch(hosts=host, verify_certs=False))
    logger.info('Elasticsearch is ready at %s', host)
