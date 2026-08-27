"""Ожидание готовности Mailpit перед запуском тестов.

Mailpit — настоящий SMTP-сервер с HTTP API поверх принятых писем. Именно этот
API и делает проверку «письмо действительно ушло» возможной в контейнерном
наборе: ``aiosmtpd`` работает внутри процесса и видит только то, что отправил
он сам, то есть для сценария «письмо отправил соседний контейнер» бесполезен.
"""

import logging
import os

from practix_testing.utils.wait_for_http import wait_for_http

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


if __name__ == '__main__':
    url = os.getenv('MAILPIT_URL', 'http://127.0.0.1:8025').rstrip('/')
    wait_for_http(f'{url}/readyz', service='Mailpit')
    logger.info('Mailpit is ready at %s', url)
