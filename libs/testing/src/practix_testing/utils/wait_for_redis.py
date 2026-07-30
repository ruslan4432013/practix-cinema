"""Ожидание готовности Redis перед запуском тестов."""

import logging
import os

from redis import Redis

# См. комментарий в wait_for_es.py — скрипт запускается файлом.
from practix_testing.utils.retry_utils import backoff

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@backoff(max_attempts=30)
def wait_for_redis(redis_client: Redis):
    if not redis_client.ping():
        raise ConnectionError('Redis is not available')


if __name__ == '__main__':
    host = os.getenv('REDIS_HOST', '127.0.0.1')
    port = int(os.getenv('REDIS_PORT', '6379'))
    wait_for_redis(Redis(host=host, port=port))
    logger.info('Redis is ready at %s:%s', host, port)
