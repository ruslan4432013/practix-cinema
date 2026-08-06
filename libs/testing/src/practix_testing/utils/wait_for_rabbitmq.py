"""Ожидание готовности RabbitMQ перед запуском тестов.

Проверяется HTTP API управления, а не TCP-порт 5672: порт открывается раньше,
чем брокер готов обслуживать AMQP, и тест успевал получить отказ на первом же
подключении. ``/api/health/checks/alarms`` отвечает 200 только когда брокер
действительно принимает работу.
"""

import logging
import os

from practix_testing.utils.wait_for_http import wait_for_http

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


if __name__ == '__main__':
    url = os.getenv('RABBITMQ_API_URL', 'http://127.0.0.1:15672').rstrip('/')
    credentials = (os.getenv('RABBITMQ_USER', 'guest'), os.getenv('RABBITMQ_PASSWORD', 'guest'))
    wait_for_http(f'{url}/api/health/checks/alarms', service='RabbitMQ', auth=credentials)
    logger.info('RabbitMQ is ready at %s', url)
