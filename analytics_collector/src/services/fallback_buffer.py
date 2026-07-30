"""Буфер деградации: приём событий продолжается, когда Kafka недоступна.

Требование спринта — сайт не должен ломаться из-за проблем в инфраструктуре
аналитики. Отдавать 503 нельзя: браузер пользователя получил бы ошибку на
каждое действие. Поэтому при недоступной Kafka событие складывается в
ограниченный по длине список Redis, а фоновая задача перекладывает накопленное
в Kafka, как только та поднимется.

Устройство:

* ``LPUSH`` в голову списка + ``LTRIM`` — жёсткая верхняя граница длины. Без неё
  многочасовой отказ Kafka привёл бы к исчерпанию памяти Redis, то есть авария
  в аналитике утянула бы за собой Auth и Movies API, которые живут в том же
  Redis. При переполнении отбрасываются **самые старые** записи: свежие данные
  для аналитики ценнее.
* Дренаж забирает записи через ``LMOVE`` в отдельный in-flight-список, а
  удаляет их только после подтверждения от брокера. Простой ``RPOP`` был бы
  ошибкой: он удаляет запись до доставки, и падение процесса (рестарт, деплой,
  OOM) между извлечением и публикацией теряет её безвозвратно — ровно в тот
  момент, когда буфер и нужен. Незавершённые записи возвращаются в очередь при
  следующем запуске дренажа.
* Дренаж работает под распределённой блокировкой: воркеров несколько, а
  осмысленно дренировать буфер должен один — иначе они конкурируют за одни и те
  же записи и множат нагрузку на брокер.
* Запись, которую не удалось доставить ``UGC_FALLBACK_MAX_ATTEMPTS`` раз при
  живом брокере, уходит в DLQ-топик: она отравлена (например, превышает
  ``max.message.bytes``), и вечные повторы заблокировали бы очередь целиком.
  Если же брокер прямо сказал, что дело в самой записи (``RecordRejectedError``),
  попытки не тратятся вовсе — она уезжает в DLQ сразу.
"""

import asyncio
import contextlib
import json
import logging
import uuid

from brokers.base import BrokerUnavailableError, EventBroker, RecordRejectedError
from core import metrics
from core.config import settings
from models.events import KafkaRecord

logger = logging.getLogger(__name__)

_LOCK_KEY = 'ugc:fallback:lock'
# Список записей, извлечённых дренажом, но ещё не подтверждённых брокером.
_PROCESSING_KEY = 'ugc:fallback:processing'
# TTL блокировки заметно больше интервала дренажа, но конечен: если воркер
# умрёт с захваченной блокировкой, она освободится сама.
_LOCK_TTL = 60

# Освобождаем блокировку только если она всё ещё наша: за время работы TTL мог
# истечь, и блокировку мог захватить другой воркер — удалять чужую нельзя.
_RELEASE_LOCK_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


class FallbackBuffer:
    """Redis-буфер недоставленных событий и его фоновый дренаж."""

    def __init__(self, redis_provider, broker: EventBroker):
        self._redis_provider = redis_provider
        self._broker = broker
        self._drain_task: asyncio.Task | None = None
        self._stopped = False

    # ------------------------------------------------------------------ запись

    async def push(self, record: KafkaRecord) -> bool:
        """Кладёт запись в буфер. Возвращает False, если и Redis недоступен."""
        if not settings.UGC_FALLBACK_ENABLED:
            return False
        redis = self._redis_provider()
        if redis is None:
            return False
        try:
            pipe = redis.pipeline()
            pipe.lpush(settings.UGC_FALLBACK_KEY, json.dumps(record.to_dict()))
            pipe.ltrim(settings.UGC_FALLBACK_KEY, 0, settings.UGC_FALLBACK_MAX_SIZE - 1)
            await pipe.execute()
        except Exception as exc:  # noqa: BLE001 — Redis недоступен, событие теряем осознанно
            logger.error('Failed to buffer event, it will be lost: %s', exc)
            return False
        return True

    async def size(self) -> int:
        redis = self._redis_provider()
        if redis is None:
            return 0
        try:
            return int(await redis.llen(settings.UGC_FALLBACK_KEY))
        except Exception:  # noqa: BLE001
            return 0

    # ------------------------------------------------------------------ дренаж

    def start(self) -> None:
        self._stopped = False
        if self._drain_task is None:
            self._drain_task = asyncio.create_task(self._drain_loop())

    async def stop(self) -> None:
        self._stopped = True
        if self._drain_task is not None:
            self._drain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._drain_task
            self._drain_task = None

    async def _drain_loop(self) -> None:
        while not self._stopped:
            try:
                await asyncio.sleep(settings.UGC_FALLBACK_DRAIN_INTERVAL)
                await self.drain_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — цикл дренажа не должен умирать
                logger.error('Fallback drain cycle failed: %s', exc)

    async def drain_once(self) -> int:
        """Один проход дренажа. Возвращает число доставленных записей."""
        redis = self._redis_provider()
        if redis is None or not settings.UGC_FALLBACK_ENABLED:
            return 0

        # Пока брокер лежит, дренировать бессмысленно — только зря дёргать Redis.
        if not self._broker.is_healthy:
            return 0

        token = str(uuid.uuid4())
        if not await self._acquire_lock(redis, token):
            return 0

        delivered = 0
        try:
            # Записи, зависшие в in-flight после аварийного завершения прошлого
            # дренажа, возвращаем в очередь — иначе они остались бы там навсегда.
            recovered = await self._recover_in_flight(redis)
            if recovered:
                logger.warning('Recovered %d in-flight records after an interrupted drain', recovered)

            for _ in range(settings.UGC_FALLBACK_DRAIN_BATCH):
                if self._stopped:
                    break
                # LMOVE атомарно переносит запись из очереди в in-flight:
                # с этого момента она либо будет доставлена, либо восстановлена,
                # но не исчезнет при падении процесса.
                raw = await redis.lmove(settings.UGC_FALLBACK_KEY, _PROCESSING_KEY, 'RIGHT', 'LEFT')
                if raw is None:
                    break
                if not await self._deliver_one(redis, raw):
                    break
                delivered += 1
        finally:
            await self._release_lock(redis, token)
            metrics.fallback_buffer_size.set(await self.size())

        if delivered:
            logger.info('Fallback drain delivered %d buffered events', delivered)
        return delivered

    async def _deliver_one(self, redis, raw: str) -> bool:
        """Доставляет одну запись. Возвращает False, если дренаж пора прервать."""
        try:
            record = KafkaRecord.from_dict(json.loads(raw))
        except (ValueError, KeyError, TypeError) as exc:
            # Испорченная запись: повторы не помогут, а блокировать ею очередь
            # нельзя — отбрасываем и фиксируем метрикой.
            logger.error('Discarding malformed record from fallback buffer: %s', exc)
            metrics.events_dropped.labels(event_type='unknown', reason='malformed_buffer_record').inc()
            await self._forget_in_flight(redis, raw)
            return True

        try:
            await self._broker.publish_now(record)
        except RecordRejectedError:
            # Брокер жив, но эту запись он не примет ни сейчас, ни через час:
            # отказ вызван её собственными свойствами (размер, формат). Тратить
            # на неё UGC_FALLBACK_MAX_ATTEMPTS проходов дренажа незачем — каждый
            # проход упирался бы в неё и не доходил до остальных записей.
            record.attempts += 1
            await self._dead_letter(record)
            await self._forget_in_flight(redis, raw)
            return True
        except BrokerUnavailableError:
            record.attempts += 1
            if record.attempts >= settings.UGC_FALLBACK_MAX_ATTEMPTS and self._broker.is_healthy:
                # Брокер жив, а конкретная запись не проходит — она отравлена
                # (например, превышает max.message.bytes).
                await self._dead_letter(record)
                await self._forget_in_flight(redis, raw)
                return True
            await self._return_to_queue(redis, raw, record)
            # Брокер недоступен — перебирать остальные записи бессмысленно.
            return False

        metrics.fallback_drained.inc()
        await self._forget_in_flight(redis, raw)
        return True

    @staticmethod
    async def _forget_in_flight(redis, raw: str) -> None:
        """Удаляет подтверждённую запись из in-flight-списка."""
        try:
            await redis.lrem(_PROCESSING_KEY, 1, raw)
        except Exception as exc:  # noqa: BLE001
            logger.error('Failed to remove record from in-flight list: %s', exc)

    @staticmethod
    async def _return_to_queue(redis, raw: str, record: KafkaRecord) -> None:
        """Возвращает недоставленную запись в очередь с обновлённым счётчиком.

        Сначала запись кладётся обратно в очередь и только потом удаляется из
        in-flight: обратный порядок при сбое между операциями потерял бы её.
        Худший случай такого порядка — дубликат, что укладывается в гарантию
        «хотя бы один раз».
        """
        try:
            await redis.rpush(settings.UGC_FALLBACK_KEY, json.dumps(record.to_dict()))
            await redis.lrem(_PROCESSING_KEY, 1, raw)
        except Exception as exc:  # noqa: BLE001
            logger.error('Failed to requeue record into fallback buffer: %s', exc)

    @staticmethod
    async def _recover_in_flight(redis) -> int:
        """Возвращает в очередь записи прерванного дренажа.

        Порядок внутри восстановленной группы не сохраняется, но все они
        оказываются в хвосте очереди, то есть будут обработаны первыми.
        """
        recovered = 0
        try:
            while True:
                moved = await redis.lmove(_PROCESSING_KEY, settings.UGC_FALLBACK_KEY, 'LEFT', 'RIGHT')
                if moved is None:
                    break
                recovered += 1
        except Exception as exc:  # noqa: BLE001
            logger.error('Failed to recover in-flight records: %s', exc)
        return recovered

    async def _dead_letter(self, record: KafkaRecord) -> None:
        """Отправляет исчерпавшую попытки запись в DLQ-топик."""
        dlq_record = KafkaRecord(
            topic=settings.KAFKA_TOPIC_DLQ,
            key=record.key,
            value=record.value,
            headers=[*record.headers, ('x-original-topic', record.topic.encode('utf-8'))],
        )
        try:
            await self._broker.publish_now(dlq_record)
            metrics.fallback_dead_lettered.inc()
            logger.warning('Record dead-lettered after %d attempts (topic %s)', record.attempts, record.topic)
        except BrokerUnavailableError as exc:
            logger.error('Failed to dead-letter record: %s', exc)
            metrics.events_dropped.labels(event_type='unknown', reason='dead_letter_failed').inc()

    # ------------------------------------------------------------------ блокировка

    @staticmethod
    async def _acquire_lock(redis, token: str) -> bool:
        try:
            return bool(await redis.set(_LOCK_KEY, token, nx=True, ex=_LOCK_TTL))
        except Exception as exc:  # noqa: BLE001
            logger.warning('Failed to acquire drain lock: %s', exc)
            return False

    @staticmethod
    async def _release_lock(redis, token: str) -> None:
        try:
            await redis.eval(_RELEASE_LOCK_SCRIPT, 1, _LOCK_KEY, token)
        except Exception as exc:  # noqa: BLE001
            logger.warning('Failed to release drain lock: %s', exc)
