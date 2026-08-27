"""Темп отправки — свойство СЕРВИСА, а не процесса.

## Зачем понадобилось общее хранилище

Ограничение скорости жило в поле экземпляра отправителя: «когда я отправлял в
прошлый раз». Пока отправляющий воркер один, это работает; но он масштабируется
репликами контейнера — так и задумано, очередь одна, консьюмеров сколько нужно.
С N репликами в почтовый сервер идёт ``N * NOTIFY_SMTP_RATE_PER_SECOND``, то
есть защитная ручка отменяется ровно тем действием, ради которого её и заводили:
добавлением мощности. А предостерегает теория именно от этого — «отправка вроде
идёт гладко, но от нагрузки падает почтовый сервер и сервис падает вслед за ним».

Поэтому темп считается в общем хранилище, и число реплик на него не влияет.

## Алгоритм: резервирование слота, а не счётчик в окне

В ключе лежит момент, в который разрешено отправить СЛЕДУЮЩЕЕ письмо. Каждый
отправитель одним атомарным скриптом сдвигает его на ``1 / rate`` и узнаёт,
сколько ему ждать до своего слота. Это ровно то, что нужно почтовому серверу, —
равномерный шаг, а не «сто писем в первую секунду минуты и тишина потом», как
даёт любое оконное ограничение.

Отсюда же короткое ожидание: слот резервируется синхронно перед ОДНИМ письмом,
поэтому очередь ожидающих не длиннее числа одновременно отправляющих процессов
(десять реплик при 50 письмах в секунду — 0.18 с в худшем случае).

Часы берутся из самого Redis (``TIME``), а не из процессов: реплики живут в
разных контейнерах, и расхождение их системного времени превратилось бы в
расхождение темпа. Арифметика — целые микросекунды, и значение кладётся через
``string.format('%d')``: Lua-числа Redis сериализует форматом ``%.14g``, а
микросекундная эпоха в него уже не помещается и превратилась бы в ``1.78e+15``.

## Что происходит, когда Redis недоступен

Пауза по локальному интервалу — то есть ровно прежнее поведение — плюс WARNING и
короткий предохранитель, чтобы во время недоступности не ходить в Redis на каждое
письмо. Рассылка не должна останавливаться из-за хранилища ручки, а деградация
направлена в единственную безопасную сторону: лимит снова становится
попроцессным, то есть не строже и не мягче, чем был до этого модуля. Прецедент
fail-open — лимитер коллектора (``core/rate_limit.py`` там же в комментарии
объясняет, почему у Auth выбран обратный ответ).
"""

import logging
import time

from redis.exceptions import RedisError

from practix_notifications.channels.base import TemporaryDeliveryError
from practix_notifications.core.config import RATE_SCOPE_GLOBAL, settings
from practix_notifications.core.redis import get_auth_redis

logger = logging.getLogger('notifications.pacing')

#: Скрипт целиком, потому что «прочитать, посчитать, записать» тремя командами —
#: это гонка: две реплики прочитали бы один и тот же слот и отправили одновременно.
RESERVE_SLOT = """
local slot_key = KEYS[1]
local interval = tonumber(ARGV[1])
local max_wait = tonumber(ARGV[2])
local ttl_ms = tonumber(ARGV[3])
local clock = redis.call('TIME')
local now = clock[1] * 1000000 + clock[2]
local slot = tonumber(redis.call('GET', slot_key)) or 0
if slot < now then slot = now end
local wait = slot - now
if wait > max_wait then return -1 end
redis.call('SET', slot_key, string.format('%d', slot + interval), 'PX', ttl_ms)
return wait
"""

#: Сколько не трогать Redis после отказа. Неудачная попытка стоит не таймаута
#: сокета, а нескольких секунд: клиент redis-py по умолчанию сам повторяет запрос
#: с нарастающей паузой. Без предохранителя эта цена платилась бы за КАЖДОЕ
#: письмо, то есть недоступный Redis чинил бы темп худшим из возможных способов.
DEGRADED_SECONDS = 30.0

#: Запас к сроку жизни ключа сверх самого дальнего слота. Ключ обязан быть
#: волатильным: Redis ядра работает в режиме volatile-lru, и вечный ключ там
#: невытесняем.
TTL_MARGIN_MS = 5_000


class RatePacer:
    """Шаг отправки, общий на все реплики сервиса.

    Ручки читаются на каждом вызове, а не запоминаются в конструкторе: отправитель
    создаётся один раз на процесс и живёт столько же, сколько сам процесс.

    :param key: ключ слота. В нём есть адрес почтового сервера, потому что лимит
        защищает КОНКРЕТНЫЙ сервер, и после смены адреса наследовать чужой слот
        незачем.
    :param client_factory: источник клиента Redis. Параметр, а не импорт, —
        чтобы юнит-набор подставил заглушку и остался проходимым на голом
        раннере, где Redis нет.
    """

    def __init__(self, *, key: str, client_factory=get_auth_redis) -> None:
        self._key = key
        self._client_factory = client_factory
        self._script = None
        self._degraded_until = 0.0
        #: Запасные часы на случай недоступного Redis — прежний попроцессный лимит.
        self._last_slot_at = 0.0

    def wait_for_slot(self) -> None:
        """Дождаться своей очереди на отправку одного сообщения.

        Бросает ``TemporaryDeliveryError``, если ждать пришлось бы дольше
        ``NOTIFY_SMTP_RATE_MAX_WAIT``: получатель уедет в штатный ярус повторов,
        а воркер не встанет на неопределённое время внутри одной пачки.
        """
        interval = 1.0 / settings.NOTIFY_SMTP_RATE_PER_SECOND
        if not self._use_shared_slot():
            self._wait_locally(interval)
            return

        try:
            wait_us = self._reserve(interval)
        except (RedisError, OSError) as exc:
            # Отказ хранилища ручки — не отказ доставки. Возвращаемся к прежнему
            # попроцессному темпу и перестаём стучаться в Redis на полминуты.
            logger.warning('Shared SMTP pace unavailable, falling back to per-process rate: %s', exc)
            self._degraded_until = time.monotonic() + DEGRADED_SECONDS
            self._wait_locally(interval)
            return

        if wait_us < 0:
            raise TemporaryDeliveryError(
                f'Очередь отправки длиннее NOTIFY_SMTP_RATE_MAX_WAIT '
                f'({settings.NOTIFY_SMTP_RATE_MAX_WAIT} с) — письмо уедет в повтор'
            )
        if wait_us:
            time.sleep(wait_us / 1_000_000)
        # Локальные часы держатся свежими и на общем пути: если Redis отвалится
        # посреди пачки, запасной лимит продолжит шаг, а не выпустит письмо сразу.
        self._last_slot_at = time.monotonic()

    def _use_shared_slot(self) -> bool:
        if settings.NOTIFY_SMTP_RATE_SCOPE != RATE_SCOPE_GLOBAL:
            return False
        return time.monotonic() >= self._degraded_until

    def _reserve(self, interval: float) -> int:
        max_wait_us = int(settings.NOTIFY_SMTP_RATE_MAX_WAIT * 1_000_000)
        interval_us = max(1, int(interval * 1_000_000))
        ttl_ms = (max_wait_us + interval_us) // 1000 + TTL_MARGIN_MS
        if self._script is None:
            # register_script даёт EVALSHA с автоматическим откатом на EVAL:
            # текст скрипта не едет по сети на каждое письмо.
            self._script = self._client_factory().register_script(RESERVE_SLOT)
        return int(self._script(keys=[self._key], args=[interval_us, max_wait_us, ttl_ms]))

    def _wait_locally(self, interval: float) -> None:
        """Прежний попроцессный шаг: он же запасной путь, он же режим ``process``."""
        elapsed = time.monotonic() - self._last_slot_at
        if self._last_slot_at and elapsed < interval:
            time.sleep(interval - elapsed)
        self._last_slot_at = time.monotonic()
