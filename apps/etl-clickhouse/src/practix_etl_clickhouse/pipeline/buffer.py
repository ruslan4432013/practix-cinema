"""Буфер одной пачки — единственное место, где ETL держит данные в памяти.

Инвариант: **в куче живёт ровно одна пачка**. Никаких очередей «на потом»,
никаких списков непрокоммиченных пачек. Пачка ограничена сверху тремя порогами
(строки, байты, время), и пока она не уехала в ClickHouse, чтение из Kafka
остановлено (см. ``pipeline/runner.py``, pause/resume).

Роль неограниченного буфера играет сама Kafka: там данные реплицированы, лежат
на диске, стоят дёшево и переживают перезапуск процесса. В куче Python те же
данные стоят в 5-10 раз дороже своего размера в байтах и теряются при первом
же OOM-kill. Поэтому «накопить в памяти, пока хранилище лежит» — это трата
памяти ради результата, который Kafka даёт бесплатно.
"""

import logging
import time
from typing import Any

from practix_etl_clickhouse.core import metrics
from practix_etl_clickhouse.core.config import settings
from practix_etl_clickhouse.transform.envelope import InvalidEvent, ParsedEvent, parse
from practix_etl_clickhouse.transform.film_views import to_film_view_row
from practix_etl_clickhouse.transform.invalid import to_invalid_row
from practix_etl_clickhouse.transform.raw import to_raw_row

logger = logging.getLogger(__name__)

# Заголовок, который коллектор проставляет сообщениям, ушедшим в DLQ.
_ORIGINAL_TOPIC_HEADER = 'x-original-topic'


def _origin_topic(headers: Any, fallback: str) -> str:
    """Исходный топик сообщения, приехавшего через DLQ."""
    if not headers:
        return fallback
    for key, value in headers:
        if key == _ORIGINAL_TOPIC_HEADER and value:
            return value.decode('utf-8', errors='replace')
    return fallback


class BatchBuffer:
    def __init__(self) -> None:
        self._reset()

    def _reset(self) -> None:
        # Переприсваивание, а не .clear(): list.clear() освобождает элементы,
        # но оставляет разросшийся внутренний массив указателей, и после одной
        # аномально большой пачки он остаётся в RSS до конца жизни процесса.
        # Это ровно тот класс «утечки», о котором спрашивает задание.
        self.raw_rows: list[tuple] = []
        self.view_rows: list[tuple] = []
        self.invalid_rows: list[tuple] = []
        # Следующий оффсет для коммита: aiokafka ждёт «откуда читать дальше».
        self.offsets: dict[Any, int] = {}
        # Диапазоны оффсетов пачки — из них считается токен идемпотентности.
        self.ranges: dict[tuple[str, int], tuple[int, int]] = {}
        self.seen_event_ids: set = set()
        self.nbytes = 0
        self.opened_at = time.monotonic()

    def clear(self) -> None:
        self._reset()

    @property
    def is_empty(self) -> bool:
        return not (self.raw_rows or self.view_rows or self.invalid_rows)

    @property
    def rows(self) -> int:
        return len(self.raw_rows) + len(self.invalid_rows)

    def add(self, tp: Any, message: Any) -> None:
        """Разбирает сообщение и складывает готовые строки.

        Трансформация выполняется сразу, а не откладывается: объект
        ``ConsumerRecord`` после этого отпускается, и байты, прочитанные из
        сокета, не живут до конца пачки в двух копиях — сырой bytes плюс
        разобранный dict.
        """
        value = message.value
        size = len(value) if value else 0
        self.nbytes += size

        # Оффсеты и диапазоны обновляем ДО разбора: они нужны и для битого
        # сообщения — иначе оно перечитывалось бы вечно.
        self.offsets[tp] = message.offset + 1
        key = (tp.topic, tp.partition)
        low, high = self.ranges.get(key, (message.offset, message.offset))
        self.ranges[key] = (min(low, message.offset), max(high, message.offset))

        metrics.messages_consumed.labels(topic=tp.topic).inc()

        parsed = parse(value)
        if isinstance(parsed, InvalidEvent):
            metrics.messages_invalid.labels(topic=tp.topic, reason=parsed.error_kind).inc()
            logger.warning(
                'Message quarantined',
                extra={
                    'topic': tp.topic,
                    'partition': tp.partition,
                    'offset': message.offset,
                    'error_kind': parsed.error_kind,
                    'error_text': parsed.error_text,
                },
            )
            self.invalid_rows.append(to_invalid_row(parsed, tp.topic, tp.partition, message.offset, message.key, value))
            return

        self._add_parsed(parsed, tp, message)

    def _add_parsed(self, parsed: ParsedEvent, tp: Any, message: Any) -> None:
        # Первый (самый дешёвый) уровень дедупликации: повтор внутри одной
        # пачки. Такое случается штатно при ретрае дренажа буфера деградации
        # в коллекторе.
        if parsed.event_id in self.seen_event_ids:
            metrics.messages_duplicate.inc()
            return
        self.seen_event_ids.add(parsed.event_id)

        self.raw_rows.append(
            to_raw_row(
                parsed,
                topic=tp.topic,
                origin_topic=_origin_topic(message.headers, tp.topic),
                partition=tp.partition,
                offset=message.offset,
            )
        )
        view_row = to_film_view_row(parsed)
        if view_row is not None:
            self.view_rows.append(view_row)

    def should_flush(self) -> str | None:
        """Причина закрыть пачку, либо None.

        Порог по времени обязателен: без него при слабом трафике события
        лежали бы в памяти неограниченно долго, и «непрерывный ETL»
        превратился бы в ночную выгрузку.
        """
        if self.is_empty:
            return None
        if self.rows >= settings.ETL_BATCH_MAX_ROWS:
            return 'size'
        if self.nbytes >= settings.ETL_BATCH_MAX_BYTES:
            return 'bytes'
        if time.monotonic() - self.opened_at >= settings.ETL_FLUSH_INTERVAL:
            return 'time'
        return None
