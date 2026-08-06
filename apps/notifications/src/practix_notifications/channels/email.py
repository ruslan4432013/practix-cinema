"""Доставка по SMTP.

## Соединение переживает пачку

Замеры из теории («Как посылать быстрее») для одного письма: установка соединения
≈ 5 с, ``login`` ≈ 0.4 с, сама отправка < 1 с, ``quit`` ≈ 0.05 с. То есть код,
который подключается на каждое письмо, тратит на подключение в пять раз больше,
чем на работу. Поэтому соединение открывается один раз и переиспользуется, а
переоткрывается только после разрыва — этот факт закреплён юнит-тестом
(``connect`` вызван один раз на N писем), а не комментарием.

## Ограничение скорости

Почтовый сервер — внешняя система, и «отправка вроде идёт гладко, но от нагрузки
падает почтовый сервер и сервис падает вслед за ним» — прямая цитата из теории.
``NOTIFY_SMTP_RATE_PER_SECOND`` держит темп; при превышении отправитель просто
ждёт.
"""

import logging
import re
import smtplib
import time
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from practix_notifications.channels.base import (
    PermanentDeliveryError,
    RenderedMessage,
    Sender,
    TemporaryDeliveryError,
)
from practix_notifications.core.config import settings
from practix_notifications.enums import Channel

logger = logging.getLogger('notifications.email')


class SmtpSender(Sender):
    channel = Channel.EMAIL.value

    def __init__(self) -> None:
        self._smtp: smtplib.SMTP | None = None
        self._last_sent_at = 0.0
        #: Счётчик подключений — его читает юнит-тест переиспользования.
        self.connects = 0

    # --- соединение -------------------------------------------------------

    def open(self) -> None:
        if self._smtp is not None:
            return
        smtp = smtplib.SMTP(
            host=settings.NOTIFY_SMTP_HOST,
            port=settings.NOTIFY_SMTP_PORT,
            timeout=settings.NOTIFY_SMTP_TIMEOUT,
        )
        if settings.NOTIFY_SMTP_USE_TLS:
            smtp.starttls()
        if settings.NOTIFY_SMTP_USER:
            smtp.login(settings.NOTIFY_SMTP_USER, settings.NOTIFY_SMTP_PASSWORD)
        self._smtp = smtp
        self.connects += 1
        logger.info('SMTP connection opened', extra={'smtp_host': settings.NOTIFY_SMTP_HOST})

    def close(self) -> None:
        if self._smtp is None:
            return
        try:
            self._smtp.quit()
        except (smtplib.SMTPException, OSError):
            # Сервер уже мог закрыть соединение — на выходе это не важно.
            logger.debug('SMTP connection did not close cleanly', exc_info=True)
        finally:
            self._smtp = None

    # --- отправка ---------------------------------------------------------

    def send(self, address: str, message: RenderedMessage) -> None:
        self._throttle()
        try:
            self._send_once(address, message)
        except smtplib.SMTPServerDisconnected:
            # Единственный случай, когда повтор делается здесь же: соединение
            # переиспользуется, и его разрыв — не отказ доставки, а исчерпание
            # ресурса. Отдавать такое в очередь повторов значило бы гонять
            # сообщение по кругу на каждом реконнекте.
            logger.warning('SMTP connection dropped, reconnecting once')
            self.close()
            try:
                self._send_once(address, message)
            except smtplib.SMTPException as exc:
                raise TemporaryDeliveryError(f'SMTP не принял письмо после переподключения: {exc}') from exc
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPNotSupportedError) as exc:
            raise PermanentDeliveryError(f'Адрес отвергнут сервером: {exc}') from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise TemporaryDeliveryError(f'SMTP недоступен: {exc}') from exc

    def _send_once(self, address: str, message: RenderedMessage) -> None:
        self.open()
        assert self._smtp is not None
        self._smtp.send_message(self._build(address, message))
        self._last_sent_at = time.monotonic()

    def _build(self, address: str, message: RenderedMessage) -> EmailMessage:
        mail = EmailMessage()
        mail['Subject'] = message.subject
        mail['From'] = formataddr((settings.NOTIFY_SMTP_FROM_NAME, settings.NOTIFY_SMTP_FROM))
        mail['To'] = address
        mail['Message-Id'] = make_msgid(domain='practix.local')
        for name, value in message.headers.items():
            mail[name] = value

        if message.is_html:
            # Текстовая часть обязательна: письмо без неё половина почтовых
            # клиентов показывает как вложение, а спам-фильтры считают подозрительным.
            mail.set_content(_strip_tags(message.body))
            mail.add_alternative(message.body, subtype='html')
        else:
            mail.set_content(message.body)
        return mail

    def _throttle(self) -> None:
        min_interval = 1.0 / settings.NOTIFY_SMTP_RATE_PER_SECOND
        elapsed = time.monotonic() - self._last_sent_at
        if self._last_sent_at and elapsed < min_interval:
            time.sleep(min_interval - elapsed)


def _strip_tags(html: str) -> str:
    """Грубый текстовый вариант HTML-письма.

    Именно грубый: полноценная конвертация — задача вёрстки письма, а не сервиса
    доставки, и тянуть ради неё html2text в образ незачем.
    """
    text = re.sub(r'(?is)<(script|style).*?>.*?</\1>', '', html)
    text = re.sub(r'(?i)<br\s*/?>|</p>', '\n', text)
    text = re.sub(r'<[^>]+>', '', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()
