"""Реестр подписчиков процесса: кому из подключённых отдать пришедший push.

## Почему реестр в памяти, а раздача — через fanout брокера

Соединение живёт в конкретном процессе конкретной реплики, и перенести его
нельзя. Значит, push обязан доехать до ВСЕХ реплик, а каждая уже решит, держит
ли она это соединение. Это делает fanout-обменник ``notifications.ws``: каждый
процесс объявляет свою ``exclusive`` + ``auto_delete`` очередь и получает копию
каждого сообщения. Никакого второго Redis pub/sub ради этого не заводится —
брокер в стенде уже есть, и он же публикует push.

Цена честная: при N репликах каждое сообщение обрабатывается N раз. При потоке
уведомлений (не аналитики) это дёшево, а альтернатива — реестр «кто где сидит» в
общем хранилище — платит тем же трафиком плюс рассинхроном при обрыве.

## Переполнение — это кадр, а не тишина

Очередь соединения ограничена. Медленный клиент (свёрнутая вкладка, мобильная
сеть) не должен раздувать память процесса, но и молча терять кадры нельзя:
человек увидит ленту с дырой и не узнает об этом. Поэтому при переполнении
самый старый кадр выбрасывается, счётчик растёт, а клиенту уходит ``desync`` —
сигнал «догони меня по ленте». Ровно та же логика, по которой у долговечности
ответом остаётся ``inbox_message``, а сокет — быстрый путь.

Модуль чистый: ни сети, ни брокера, ни настроек. Всё, что ему нужно, приходит
аргументами — поэтому он целиком покрывается юнит-тестами без инфраструктуры.
"""

import asyncio
from collections import defaultdict
from typing import Any


class ConnectionLimitReached(Exception):
    """Лимит соединений — процесса целиком или одного пользователя."""


class Subscription:
    """Один канал доставки: открытый сокет или ждущий long-poll-запрос.

    ``dropped`` не сбрасывается при чтении сам: его снимает тот, кто отправил
    ``desync``, — иначе два конкурирующих читателя обнулили бы счётчик друг другу.
    """

    __slots__ = ('counted', 'dropped', 'queue', 'user_id')

    def __init__(self, user_id: str, *, maxsize: int, counted: bool = True) -> None:
        self.user_id = user_id
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0
        #: Занимает ли эта подписка место в бюджете соединений. Флаг хранится
        #: здесь, а не у вызывающего, чтобы ``unsubscribe`` списывал ровно то,
        #: что начислил ``subscribe``.
        self.counted = counted

    def offer(self, frame: dict[str, Any]) -> None:
        """Положить кадр, вытеснив самый старый при переполнении."""
        while True:
            try:
                self.queue.put_nowait(frame)
                return
            except asyncio.QueueFull:
                try:
                    self.queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover — гонка между двумя offer
                    continue
                self.dropped += 1

    def take_dropped(self) -> int:
        """Забрать и обнулить счётчик потерянных кадров."""
        dropped, self.dropped = self.dropped, 0
        return dropped


class ConnectionHub:
    """Кто из подключённых к ЭТОМУ процессу ждёт уведомлений."""

    def __init__(
        self,
        *,
        queue_size: int,
        max_per_user: int,
        max_total: int,
        max_pollers_per_user: int,
        max_pollers: int,
    ) -> None:
        self._queue_size = queue_size
        self._max_per_user = max_per_user
        self._max_total = max_total
        self._max_pollers_per_user = max_pollers_per_user
        self._max_pollers = max_pollers
        self._subscribers: dict[str, set[Subscription]] = defaultdict(set)
        self._total = 0
        self._pollers = 0

    @property
    def total(self) -> int:
        """Открытые сокеты. Они расходуют бюджет соединений — и только его."""
        return self._total

    @property
    def pollers(self) -> int:
        """Ждущие long-poll-запросы. У них свой бюджет, не пересекающийся с сокетами."""
        return self._pollers

    def users(self) -> int:
        return len(self._subscribers)

    def count_for(self, user_id: str) -> int:
        """Сокеты пользователя. Поллеры сюда НЕ входят: иначе висящий ``/poll``
        мешал бы тому же человеку открыть настоящее соединение — ровно то, чего
        раздельные бюджеты и должны не допускать."""
        return sum(1 for subscription in self._subscribers.get(user_id, ()) if subscription.counted)

    def pollers_for(self, user_id: str) -> int:
        """Ждущие long-poll-запросы пользователя."""
        return sum(1 for subscription in self._subscribers.get(user_id, ()) if not subscription.counted)

    def subscribe(self, user_id: str, *, count_towards_limits: bool = True) -> Subscription:
        """Зарегистрировать канал доставки.

        :param count_towards_limits: long-poll-запрос живёт секунды и по смыслу
            не «ещё одна вкладка»; считать его наравне с сокетом значило бы
            отказывать клиенту, который как раз ДЕГРАДИРОВАЛ и пытается получить
            хоть что-то.

            Поэтому у поллеров СВОЙ счётчик и СВОИ потолки. Инвариант — два
            бюджета не пересекаются ни в одну сторону: поллер не занимает слот
            сокета (иначе достаточное число одновременных ``/poll`` упёрло бы
            счётчик в ``max_total``, и деградировавший клиент выбивал бы
            недеградировавших), а сокет не занимает слот поллера.

            Безлимитными поллеры при этом быть не могут: каждый держит задачу и
            очередь на ``queue_size`` кадров до полуминуты, так что один
            аутентифицированный клиент без потолка исчерпал бы память процесса.
        """
        if count_towards_limits:
            if self._total >= self._max_total:
                raise ConnectionLimitReached(f'gateway connection limit reached ({self._max_total})')
            if self.count_for(user_id) >= self._max_per_user:
                raise ConnectionLimitReached(f'connection limit reached for user ({self._max_per_user})')
        else:
            if self._pollers >= self._max_pollers:
                raise ConnectionLimitReached(f'gateway long-poll limit reached ({self._max_pollers})')
            if self.pollers_for(user_id) >= self._max_pollers_per_user:
                raise ConnectionLimitReached(f'long-poll limit reached for user ({self._max_pollers_per_user})')

        subscription = Subscription(user_id, maxsize=self._queue_size, counted=count_towards_limits)
        self._subscribers[user_id].add(subscription)
        if subscription.counted:
            self._total += 1
        else:
            self._pollers += 1
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        """Снять канал с учёта. Идемпотентно: закрытие сокета бывает двойным."""
        bucket = self._subscribers.get(subscription.user_id)
        if bucket is None or subscription not in bucket:
            return
        bucket.discard(subscription)
        if subscription.counted:
            self._total -= 1
        else:
            self._pollers -= 1
        if not bucket:
            # Пустой набор не оставляем: иначе defaultdict превратится в
            # список всех, кто когда-либо подключался, и будет расти вечно.
            del self._subscribers[subscription.user_id]

    def publish(self, user_id: str, frame: dict[str, Any]) -> int:
        """Разослать кадр всем каналам пользователя. Возвращает, скольким досталось."""
        bucket = self._subscribers.get(user_id)
        if not bucket:
            return 0
        # Копия набора: получатель может отписаться прямо во время рассылки.
        for subscription in tuple(bucket):
            subscription.offer(frame)
        return len(bucket)
