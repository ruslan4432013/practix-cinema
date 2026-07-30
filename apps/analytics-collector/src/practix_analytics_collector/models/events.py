"""Канонический формат события, который уходит в Kafka.

Схема намеренно отделена от входных DTO (``models/requests.py``): публичный
контракт ingest-API и внутренний контракт хранилища аналитики живут своей
жизнью и версионируются независимо. Клиент может прислать поле в удобной ему
форме, а в топик попадёт обогащённый и нормализованный envelope.

Структура — «конверт + payload», как в Snowplow и Segment: неизменные
идентификационные поля лежат на верхнем уровне и одинаковы для всех типов, а
специфика типа события — внутри ``payload``. Это позволяет консьюмеру
разбирать общие поля, не зная про конкретный тип события.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from practix_analytics_collector.models.enums import DeviceType, EventType
from practix_contracts.v1 import SCHEMA_VERSION as _SCHEMA_VERSION
from practix_contracts.v1.partition_key import partition_key as _partition_key

# Версия схемы конверта. Увеличивается при несовместимом изменении структуры;
# консьюмеры обязаны сверяться с ней, а не полагаться на наличие полей. Значение
# берётся из контракта: своя константа здесь означала бы, что коллектор и ETL
# могут разойтись в понимании того, какая версия сейчас пишется.
SCHEMA_VERSION = _SCHEMA_VERSION


class EventContext(BaseModel):
    """Контекст события: часть от клиента, часть выведена сервером."""

    model_config = ConfigDict(extra='forbid')

    # --- Данные клиента ---
    url: str | None = None
    referrer: str | None = None
    screen_width: int | None = None
    screen_height: int | None = None
    viewport_width: int | None = None
    viewport_height: int | None = None
    locale: str | None = None
    timezone: str | None = None

    # --- Данные, выведенные сервером (клиент на них не влияет) ---
    user_agent: str | None = Field(default=None, description='User-Agent, усечённый до безопасной длины')
    device_type: DeviceType = DeviceType.UNKNOWN
    os: str | None = None
    browser: str | None = None
    # Необратимый солёный хеш IP. Сырой адрес не покидает процесс сервиса:
    # аналитике нужна возможность различать клиентов, а не знать их адреса.
    ip_hash: str | None = None


class EventEnvelope(BaseModel):
    """Событие в том виде, в котором оно публикуется в Kafka."""

    model_config = ConfigDict(extra='forbid')

    event_id: UUID = Field(description='Идентификатор события, ключ идемпотентности')
    event_type: EventType
    schema_version: int = SCHEMA_VERSION

    # Время клиента (может быть неточным) и время сервера (авторитетное).
    event_timestamp: datetime | None = None
    received_at: datetime

    # user_id проставляется ТОЛЬКО из проверенного JWT. Клиент не может его задать.
    user_id: UUID | None = None
    is_authenticated: bool = False
    anonymous_id: str | None = None
    session_id: str

    context: EventContext
    payload: dict[str, Any] = Field(default_factory=dict, description='Поля, специфичные для типа события')

    def partition_key(self) -> str:
        """Ключ партиционирования Kafka (правило — в practix_contracts).

        Приоритет ``user_id`` → ``anonymous_id`` → ``session_id``. Все события
        одного пользователя попадают в одну партицию, а значит читаются строго
        в порядке записи — это нужно для восстановления пути пользователя по
        сайту. Обоснование выбора ключа — в docs/architecture.md.
        """
        return _partition_key(self.user_id, self.anonymous_id, self.session_id)


@dataclass(slots=True)
class KafkaRecord:
    """Готовая к отправке запись Kafka.

    Отдельный от ``EventEnvelope`` тип нужен потому, что буфер деградации
    хранит именно запись целиком (топик + ключ + заголовки + тело), а не
    событие: при дренаже нужно воспроизвести отправку байт-в-байт, не
    пересобирая конверт заново.
    """

    topic: str
    key: str
    value: bytes
    headers: list[tuple[str, bytes]] = field(default_factory=list)
    # Сколько раз дренаж уже пытался доставить эту запись.
    attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Сериализация для хранения в Redis (bytes → latin-1-safe строки)."""
        return {
            'topic': self.topic,
            'key': self.key,
            # value — UTF-8-сериализованный JSON, поэтому декодируется без потерь.
            'value': self.value.decode('utf-8'),
            'headers': [[name, raw.decode('utf-8', errors='replace')] for name, raw in self.headers],
            'attempts': self.attempts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'KafkaRecord':
        return cls(
            topic=data['topic'],
            key=data['key'],
            value=data['value'].encode('utf-8'),
            headers=[(name, raw.encode('utf-8')) for name, raw in data.get('headers', [])],
            attempts=int(data.get('attempts', 0)),
        )
