"""Ожидание готовности Kafka перед запуском тестов.

Compose-healthcheck уже гарантирует, что брокер отвечает, но между «отвечает» и
«готов принимать записи» есть зазор: метаданные только что созданных топиков
могут ещё не разойтись. Поэтому проверка идёт не пингом, а получением списка
топиков — по образцу wait_for_es.py / wait_for_redis.py.

Используется aiokafka (она и так зависимость сервиса), а не kafka-python:
тащить в образ второй клиент Kafka ради ожидания в тестах незачем.
"""

import asyncio
import contextlib
import os
import sys

from aiokafka import AIOKafkaConsumer
from aiokafka.errors import KafkaError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BOOTSTRAP = os.environ.get('KAFKA_BOOTSTRAP_SERVERS', 'kafka:9092')
EXPECTED_TOPICS = {
    os.environ.get('KAFKA_TOPIC_CLICKS', 'ugc.clicks.v1'),
    os.environ.get('KAFKA_TOPIC_PAGE_VIEWS', 'ugc.page_views.v1'),
    os.environ.get('KAFKA_TOPIC_VIDEO_EVENTS', 'ugc.video_events.v1'),
    os.environ.get('KAFKA_TOPIC_VIDEO_PROGRESS', 'ugc.video_progress.v1'),
    os.environ.get('KAFKA_TOPIC_SEARCH_EVENTS', 'ugc.search_events.v1'),
}

MAX_ATTEMPTS = 30
DELAY_SECONDS = 2


async def wait_for_kafka() -> None:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        consumer = AIOKafkaConsumer(bootstrap_servers=BOOTSTRAP.split(','))
        try:
            await consumer.start()
            topics = await consumer.topics()
            missing = EXPECTED_TOPICS - set(topics)
            if not missing:
                print(f'Kafka is ready at {BOOTSTRAP}, topics: {sorted(EXPECTED_TOPICS)}')
                return
            print(f'[{attempt}/{MAX_ATTEMPTS}] Topics are not ready yet: {sorted(missing)}')
        except (KafkaError, OSError) as exc:
            print(f'[{attempt}/{MAX_ATTEMPTS}] Kafka is not available yet: {exc}')
        finally:
            with contextlib.suppress(Exception):
                await consumer.stop()
        await asyncio.sleep(DELAY_SECONDS)

    raise SystemExit(f'Kafka at {BOOTSTRAP} did not become ready in time')


if __name__ == '__main__':
    asyncio.run(wait_for_kafka())
