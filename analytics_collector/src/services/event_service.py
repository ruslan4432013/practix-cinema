"""Прикладной слой: превращение запроса в событие и его публикация.

Здесь сосредоточена вся логика обогащения: сервис берёт то, что прислал
клиент, добавляет то, что знает только он (личность пользователя из токена,
серверное время, производные от IP и User-Agent), и собирает канонический
конверт для Kafka.

Порядок обработки одного события:

1. Дедупликация по ``event_id`` — гасит повторы, которые неизбежны при
   ретраях браузера и при доставке ``at least once`` из буфера.
2. Сборка конверта: ``user_id`` берётся **только** из проверенного JWT.
3. Публикация в Kafka; при недоступности брокера — в буфер деградации;
   если недоступен и он — событие теряется, но фиксируется метрикой.

Ни один из шагов не может привести к 5xx: любой отказ инфраструктуры
превращается в статус доставки, а не в ошибку для клиента.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from brokers.base import BrokerUnavailableError, EventBroker
from brokers.topics import topic_for
from core import metrics
from core.config import settings
from core.privacy import hash_ip, parse_client_hints, truncate_user_agent
from models.enums import DeliveryStatus, DeviceType, EventType
from models.events import SCHEMA_VERSION, EventContext, EventEnvelope, KafkaRecord
from models.requests import BaseEventIn, VideoCompletedIn, VideoProgressIn
from services.fallback_buffer import FallbackBuffer

logger = logging.getLogger(__name__)

# Поля конверта, которые не должны дублироваться внутри payload.
_ENVELOPE_FIELDS = frozenset(
    {'event_id', 'event_type', 'event_timestamp', 'session_id', 'anonymous_id', 'context', 'properties'}
)

# Ограничение длины значения заголовка Kafka: заголовки передаются с каждым
# сообщением, и раздутый заголовок дорого обходится на больших объёмах.
_MAX_HEADER_VALUE_LENGTH = 256


@dataclass(slots=True)
class Principal:
    """Личность отправителя, установленная сервером по JWT."""

    user_id: UUID | None = None
    is_authenticated: bool = False


@dataclass(slots=True)
class RequestContext:
    """Данные запроса, которые видит только сервер."""

    ip: str | None = None
    user_agent: str | None = None
    request_id: str | None = None


@dataclass(slots=True)
class IngestResult:
    """Результат обработки одного события.

    ``event_id`` возвращается клиенту всегда: если сервер сгенерировал его сам,
    клиенту нужно знать значение, чтобы повторить отправку идемпотентно.
    """

    event_id: UUID
    status: DeliveryStatus


class EventService:
    """Приём, обогащение и публикация пользовательских событий."""

    def __init__(self, broker: EventBroker, fallback: FallbackBuffer, redis_provider):
        self._broker = broker
        self._fallback = fallback
        self._redis_provider = redis_provider

    async def ingest(
        self,
        event_in: BaseEventIn,
        principal: Principal,
        request_context: RequestContext,
    ) -> IngestResult:
        """Обрабатывает одно событие и возвращает результат его доставки."""
        event_type = EventType(event_in.event_type)
        metrics.events_received.labels(event_type=event_type.value).inc()

        envelope = self._build_envelope(event_in, principal, request_context)

        # Отсечение ботов идёт ДО дедупликации и публикации: смысл фильтра в
        # том, чтобы не платить за приём, передачу и хранение мусора, а не в
        # том, чтобы вычищать его в хранилище задним числом. Краулер, обходящий
        # каталог, даёт десятки тысяч «просмотров страниц», которые иначе
        # пришлось бы исключать в каждом аналитическом запросе.
        if settings.UGC_DROP_BOT_EVENTS and envelope.context.device_type is DeviceType.BOT:
            metrics.events_filtered.labels(event_type=event_type.value, reason='bot').inc()
            return IngestResult(envelope.event_id, DeliveryStatus.FILTERED)

        if await self._is_duplicate(envelope.event_id):
            metrics.events_duplicated.labels(event_type=event_type.value).inc()
            return IngestResult(envelope.event_id, DeliveryStatus.DUPLICATE)

        record = self._build_record(envelope, request_context)

        try:
            await self._broker.publish(record)
        except BrokerUnavailableError:
            if await self._fallback.push(record):
                metrics.events_buffered.labels(event_type=event_type.value).inc()
                return IngestResult(envelope.event_id, DeliveryStatus.BUFFERED)
            metrics.events_dropped.labels(event_type=event_type.value, reason='broker_and_buffer_down').inc()
            return IngestResult(envelope.event_id, DeliveryStatus.DROPPED)

        metrics.events_published.labels(event_type=event_type.value, topic=record.topic).inc()
        return IngestResult(envelope.event_id, DeliveryStatus.ACCEPTED)

    async def ingest_batch(
        self,
        events: list[BaseEventIn],
        principal: Principal,
        request_context: RequestContext,
    ) -> list[IngestResult]:
        """Обрабатывает пачку событий.

        События обрабатываются последовательно, а не через ``asyncio.gather``:
        публикация — это запись в локальный буфер продюсера, конкурентность
        здесь ничего не ускоряет, зато последовательный проход сохраняет
        исходный порядок событий пачки в пределах одной партиции.
        """
        return [await self.ingest(event, principal, request_context) for event in events]

    # ------------------------------------------------------------------ сборка конверта

    def _build_envelope(
        self,
        event_in: BaseEventIn,
        principal: Principal,
        request_context: RequestContext,
    ) -> EventEnvelope:
        device_type, os_family, browser_family = parse_client_hints(request_context.user_agent)
        client_context = event_in.context

        context = EventContext(
            url=client_context.url if client_context else None,
            referrer=client_context.referrer if client_context else None,
            screen_width=client_context.screen_width if client_context else None,
            screen_height=client_context.screen_height if client_context else None,
            viewport_width=client_context.viewport_width if client_context else None,
            viewport_height=client_context.viewport_height if client_context else None,
            locale=client_context.locale if client_context else None,
            timezone=client_context.timezone if client_context else None,
            user_agent=truncate_user_agent(request_context.user_agent),
            device_type=device_type,
            os=os_family,
            browser=browser_family,
            ip_hash=hash_ip(request_context.ip),
        )

        return EventEnvelope(
            # Если клиент не задал event_id, генерируем: без него невозможна
            # дедупликация при повторной доставке из буфера.
            event_id=event_in.event_id or uuid4(),
            event_type=EventType(event_in.event_type),
            schema_version=SCHEMA_VERSION,
            event_timestamp=event_in.event_timestamp,
            received_at=datetime.now(UTC),
            # Единственный источник user_id — проверенный токен.
            user_id=principal.user_id,
            is_authenticated=principal.is_authenticated,
            anonymous_id=event_in.anonymous_id,
            session_id=event_in.session_id,
            context=context,
            payload=self._build_payload(event_in),
        )

    @staticmethod
    def _build_payload(event_in: BaseEventIn) -> dict:
        """Выделяет специфичные для типа события поля."""
        payload = event_in.model_dump(mode='json', exclude=_ENVELOPE_FIELDS, exclude_none=True)
        if event_in.properties:
            payload['properties'] = event_in.properties
        # Доля просмотра считается сервером из длительностей: присланному
        # клиентом проценту доверять нельзя, а аналитике он нужен постоянно.
        # Считаем и для меток прогресса, и для «досмотрел» — тогда ETL получает
        # уже готовое поле и не дублирует эту арифметику у себя.
        if isinstance(event_in, (VideoCompletedIn, VideoProgressIn)):
            payload['completion_rate'] = event_in.completion_rate
        return payload

    def _build_record(self, envelope: EventEnvelope, request_context: RequestContext) -> KafkaRecord:
        headers: list[tuple[str, bytes]] = [
            ('event_type', envelope.event_type.value.encode('utf-8')),
            ('event_id', str(envelope.event_id).encode('utf-8')),
            ('schema_version', str(envelope.schema_version).encode('utf-8')),
        ]
        if request_context.request_id:
            headers.append(('x-request-id', self._safe_header(request_context.request_id)))

        # ``traceparent`` здесь НЕ проставляется намеренно: его добавляет
        # AIOKafkaInstrumentor в момент отправки, привязывая к собственному
        # спану продюсера. Ручная инъекция дала бы второй заголовок с тем же
        # именем — формально это допустимо, но консьюмер прочитал бы первый
        # попавшийся и восстановил бы не ту связь в трейсе.

        return KafkaRecord(
            topic=topic_for(envelope.event_type),
            key=envelope.partition_key(),
            value=envelope.model_dump_json(exclude_none=False).encode('utf-8'),
            headers=headers,
        )

    @staticmethod
    def _safe_header(value: str) -> bytes:
        """Готовит значение заголовка Kafka.

        Заголовок частично приходит извне (``x-request-id``), поэтому длина
        ограничивается, а непечатаемые символы вычищаются.
        """
        cleaned = ''.join(ch for ch in value[:_MAX_HEADER_VALUE_LENGTH] if ch.isprintable())
        return cleaned.encode('utf-8', errors='replace')

    # ------------------------------------------------------------------ дедупликация

    async def _is_duplicate(self, event_id: UUID) -> bool:
        """Проверяет, приходило ли уже событие с таким ``event_id``.

        ``SET NX EX`` атомарен, поэтому проверка корректна и при нескольких
        воркерах. Недоступность Redis трактуется как «не дубликат»: пропустить
        дубль лучше, чем потерять событие (гарантия — at least once).
        """
        if not settings.UGC_DEDUP_ENABLED:
            return False
        redis = self._redis_provider()
        if redis is None:
            return False
        try:
            created = await redis.set(f'ugc:dedup:{event_id}', '1', nx=True, ex=settings.UGC_DEDUP_TTL)
        except Exception as exc:  # noqa: BLE001 — дедупликация не критична
            logger.warning('Deduplication check skipped, Redis unavailable: %s', exc)
            return False
        return not created
