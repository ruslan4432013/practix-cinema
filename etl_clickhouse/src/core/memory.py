"""Мониторинг памяти процесса.

Задание требует убедиться, что при непрерывном потоке приложение не «течёт».
Важна не разовая цифра, а форма кривой: RSS обязан выходить на плато. Поэтому
измерений три, и каждое врёт по-своему:

* **RSS** (``/proc/self/statm``, он же ``process_resident_memory_bytes`` из
  ProcessCollector) — то, что видит ОС. Но он включает память, которую
  аллокатор Python уже освободил, а ядру не вернул, поэтому «RSS не падает»
  само по себе утечкой не является.
* **``sys.getallocatedblocks()``** — число живых блоков CPython. От поведения
  аллокатора не зависит и растёт монотонно ровно тогда, когда объекты
  действительно не освобождаются. Стоит O(1), поэтому снимается всегда и
  служит основным индикатором.
* **``tracemalloc``** — единственный источник, отвечающий на вопрос «ГДЕ
  течёт». Примерно удваивает стоимость каждой аллокации, поэтому включается
  флагом ``ETL_TRACEMALLOC_ENABLED`` — на время расследования, а не постоянно.

Сознательно НЕ используется ``len(gc.get_objects())``: вызов обходит всю кучу
и аллоцирует список на миллионы элементов, то есть измерение испортило бы
измеряемое — и заодно дало бы всплеск той самой метрики, которую измеряет.
"""

import asyncio
import logging
import resource
import sys
import tracemalloc

from core import metrics
from core.config import settings

logger = logging.getLogger(__name__)

_BYTES_IN_MB = 1024 * 1024
# Кадры стека, которые нужны, чтобы отличить «список растёт в buffer.add» от
# «список растёт где-то в aiokafka». Больше одного кадра резко удорожает
# tracemalloc, меньше — делает вывод бесполезным.
_TRACEMALLOC_FRAMES = 1


def read_rss_bytes() -> int | None:
    """RSS процесса в байтах.

    ``/proc/self/statm`` есть только на Linux (боевой и тестовый стенд), при
    локальном запуске на macOS его нет — там берём пиковый RSS из
    ``getrusage``. Единицы у ``ru_maxrss`` разные: на Linux килобайты, на
    macOS байты, поэтому платформу приходится различать явно.
    """
    try:
        with open('/proc/self/statm', 'rb') as handle:
            pages = int(handle.read().split()[1])
        return pages * resource.getpagesize()
    except (OSError, IndexError, ValueError):
        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return usage if sys.platform == 'darwin' else usage * 1024


class MemoryWatcher:
    """Периодически снимает показатели памяти в метрики и логи."""

    def __init__(self) -> None:
        self._prev_snapshot: tracemalloc.Snapshot | None = None
        self._tracemalloc_started = False

    def start(self) -> None:
        """Включает tracemalloc, если он запрошен.

        Вызывать нужно ДО создания консьюмера и клиента ClickHouse: иначе
        аллокации, сделанные при старте, не попадут в базовую линию и будут
        выглядеть отсутствующими, а не постоянными.
        """
        if settings.ETL_TRACEMALLOC_ENABLED and not tracemalloc.is_tracing():
            tracemalloc.start(_TRACEMALLOC_FRAMES)
            self._tracemalloc_started = True
            logger.info('tracemalloc enabled (frames=%d)', _TRACEMALLOC_FRAMES)

    def sample(self) -> None:
        """Снимает дешёвые показатели. Вызывается часто."""
        metrics.allocated_blocks.set(sys.getallocatedblocks())

        rss = read_rss_bytes()
        if rss is not None and rss > settings.ETL_RSS_WARN_MB * _BYTES_IN_MB:
            logger.warning(
                'RSS above threshold',
                extra={'rss_mb': round(rss / _BYTES_IN_MB, 1), 'threshold_mb': settings.ETL_RSS_WARN_MB},
            )

        if tracemalloc.is_tracing():
            traced, peak = tracemalloc.get_traced_memory()
            metrics.tracemalloc_traced_bytes.set(traced)
            metrics.tracemalloc_peak_bytes.set(peak)

    def diff_snapshot(self) -> None:
        """Логирует топ приростов аллокаций с прошлого снимка.

        Именно diff, а не абсолютный топ: постоянно занятая память (кеши,
        загруженные модули) утечкой не является, а интересен только прирост.

        Топ уходит в лог, а не в метрику с меткой на строку кода: строки в
        топе меняются от снимка к снимку, и такая метка взорвала бы
        кардинальность Prometheus.
        """
        if not tracemalloc.is_tracing():
            return

        snapshot = tracemalloc.take_snapshot().filter_traces(
            (
                tracemalloc.Filter(False, tracemalloc.__file__),
                tracemalloc.Filter(False, '<frozen importlib._bootstrap>'),
                tracemalloc.Filter(False, __file__),
            )
        )
        if self._prev_snapshot is not None:
            top = snapshot.compare_to(self._prev_snapshot, 'lineno')[: settings.ETL_TRACEMALLOC_TOP_N]
            logger.info(
                'tracemalloc top allocations diff',
                extra={
                    'top_allocations': [
                        {
                            'file': stat.traceback[0].filename,
                            'line': stat.traceback[0].lineno,
                            'size_diff_kb': round(stat.size_diff / 1024, 1),
                            'count_diff': stat.count_diff,
                        }
                        for stat in top
                    ]
                },
            )
        # Держим РОВНО один предыдущий снимок: список снимков сам стал бы
        # утечкой — тем самым, что мы ищем.
        self._prev_snapshot = snapshot

    async def run(self, stop_event: asyncio.Event) -> None:
        """Фоновая задача: дешёвые замеры часто, tracemalloc-diff редко."""
        elapsed_since_diff = 0.0
        interval = settings.ETL_MEMORY_CHECK_INTERVAL
        while not stop_event.is_set():
            try:
                self.sample()
                elapsed_since_diff += interval
                if elapsed_since_diff >= settings.ETL_TRACEMALLOC_INTERVAL:
                    elapsed_since_diff = 0.0
                    self.diff_snapshot()
            except Exception:
                logger.exception('Memory watcher iteration failed')

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except TimeoutError:
                continue

    def stop(self) -> None:
        if self._tracemalloc_started and tracemalloc.is_tracing():
            tracemalloc.stop()
        self._prev_snapshot = None
