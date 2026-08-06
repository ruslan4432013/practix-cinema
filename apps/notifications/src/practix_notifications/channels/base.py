"""Контракт отправителя: одна точка расширения на все способы доставки.

Требование задания — «система должна иметь возможность расширения на другие типы
уведомлений: смс, push и другие варианты». Выполняется оно тем, что весь путь
рассылки — от кампании до задачи доставки — оперирует ``Channel``, а знание о том,
как именно доставить, живёт ровно в одном классе на канал. Добавить SMS значит
написать ``SmsSender`` и зарегистрировать его; ни воркер, ни модели, ни админка не
меняются.

Разделение ``open``/``send``/``close`` не косметическое: у email установка
соединения стоит около пяти секунд против доли секунды на само письмо (замер из
теории, «Как посылать быстрее»), поэтому отправитель обязан иметь место, где
соединение переживает пачку.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class ChannelNotImplemented(Exception):
    """Канал объявлен, но отправителя у него нет."""


class TemporaryDeliveryError(Exception):
    """Отказ, который лечится повтором: сеть, таймаут, 4xx почтового сервера."""


class PermanentDeliveryError(Exception):
    """Отказ, который повтором не лечится: адреса не существует."""


@dataclass(frozen=True)
class RenderedMessage:
    """Готовое к отправке сообщение — уже собранный текст, без шаблонов."""

    subject: str
    body: str
    is_html: bool = True
    #: Технические заголовки: X-Request-Id, X-Notification-Id, X-Idempotency-Key.
    #: По последнему функциональный тест доказывает, что письмо ушло ровно один
    #: раз, а не просто «писем оказалось столько же, сколько ожидали».
    headers: dict[str, str] = field(default_factory=dict)


class Sender(ABC):
    """Отправитель одного канала."""

    channel: str

    # Оба хука ниже — НЕОБЯЗАТЕЛЬНЫЕ точки расширения, и подавление B027 на них
    # осознанное: сделать их абстрактными значило бы заставить каждый канал
    # писать пустую заглушку ради отправителя, у которого нет состояния.
    def open(self) -> None:  # noqa: B027
        """Подготовить ресурсы (соединение, клиент). Необязательно."""

    def close(self) -> None:  # noqa: B027
        """Освободить ресурсы. Необязательно."""

    @abstractmethod
    def send(self, address: str, message: RenderedMessage) -> None:
        """Доставить сообщение. Бросает ``TemporaryDeliveryError`` /
        ``PermanentDeliveryError`` — по ним воркер решает, повторять или нет."""

    def __enter__(self) -> 'Sender':
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class UnimplementedSender(Sender):
    """Заглушка канала, у которого ещё нет реализации.

    Существует, чтобы отказ был ЯВНЫМ и попадал в статистику задачи
    (``skip_reason='channel_unavailable'``), а не выглядел как молчаливая потеря
    письма.
    """

    def __init__(self, channel: str, reason: str) -> None:
        self.channel = channel
        self._reason = reason

    def send(self, address: str, message: RenderedMessage) -> None:
        raise ChannelNotImplemented(f'Канал {self.channel} не реализован: {self._reason}')
