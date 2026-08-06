"""Конверт сообщения — контракт между планировщиком, веером и воркером.

Чистый модуль: ни pika, ни Django. Его импортируют обе стороны обмена и тесты.

## Конверт — это обещание

Теория формулирует правило для отчётных событий: «Добавление поля не так страшно,
как его удаление... Ещё более страшное — изменение семантики поля». Отсюда два
следствия, зашитые в код:

* ``build_*`` собирает МИНИМАЛЬНЫЙ набор полей, а не «всё, что есть в модели»:
  выгрузив объект целиком, мы обязались бы поддерживать его форму вечно;
* ``parse`` НЕ ПАДАЕТ на незнакомых ключах. Продюсер новее консьюмера — штатная
  ситуация при поочерёдном деплое, и консьюмер, падающий на лишнем поле,
  превращает её в остановку рассылки.

Несовместимость выражается ``schema_version``: на неизвестную МАЖОРНУЮ версию
консьюмер отвечает отказом разбирать, а не догадками.

## Два разных сообщения на пути письма

``notification.requested`` — «этих людей надо уведомить». Внутри только
идентификаторы: ``task_id``, ``idempotency_key``, ``subscriber_id``. Ни адреса,
ни имени, ни таймзоны здесь нет и быть не должно — за личными данными
формирующий воркер идёт в Auth сам.

``notification.prepared`` — «письмо собрано, отправьте». Внутри готовые
``subject``/``body`` и адрес, но уже нет ни ``context``, ни ``template``:
тащить входные данные завершённого рендера — та самая «выгрузка объекта
целиком», от которой предостерегает правило выше.

Список получателей в ``.requested`` лежит под ключом ``targets``, а НЕ под
переименованным ``recipients``: старый консьюмер, встретив новое сообщение,
найдёт пустой список и ничего не сделает — вместо того чтобы разобрать его
наполовину и отправить письмо без имени. Изменить смысл существующего поля
хуже, чем завести новое.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

SCHEMA_VERSION = 1

#: Ключи, без которых сообщение бессмысленно. Набор закреплён тестом.
REQUIRED_FIELDS = ('schema_version', 'event_id', 'occurred_at', 'type', 'run_id')

EVENT_CAMPAIGN_LAUNCHED = 'campaign.launched'
EVENT_NOTIFICATION_REQUESTED = 'notification.requested'
EVENT_NOTIFICATION_PREPARED = 'notification.prepared'
EVENT_NOTIFICATION_DELIVERED = 'notification.delivered'
EVENT_NOTIFICATION_FAILED = 'notification.failed'


class EnvelopeError(ValueError):
    """Сообщение невозможно разобрать — отправляется в dead-letters, не в retry."""


@dataclass(frozen=True)
class TargetRef:
    """Кого уведомить — ТОЛЬКО идентификаторы.

    Ни адреса, ни ФИО: их формирующий воркер запрашивает у Auth сам. Это
    требование задания, и оно же — здравый смысл: адрес, застывший в очереди на
    сутки, может уже не принадлежать этому человеку.
    """

    task_id: str
    idempotency_key: str
    subscriber_id: str

    def as_dict(self) -> dict[str, Any]:
        return {
            'task_id': self.task_id,
            'idempotency_key': self.idempotency_key,
            'subscriber_id': self.subscriber_id,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> 'TargetRef':
        try:
            return cls(
                task_id=str(raw['task_id']),
                idempotency_key=str(raw['idempotency_key']),
                subscriber_id=str(raw['subscriber_id']),
            )
        except (KeyError, TypeError) as exc:
            raise EnvelopeError(f'Получатель описан неполно: {exc}') from exc


@dataclass(frozen=True)
class PreparedMessage:
    """Готовое письмо конкретному человеку: осталось только отправить."""

    task_id: str
    idempotency_key: str
    subscriber_id: str
    address: str
    subject: str
    body: str

    def as_dict(self) -> dict[str, Any]:
        return {
            'task_id': self.task_id,
            'idempotency_key': self.idempotency_key,
            'subscriber_id': self.subscriber_id,
            'address': self.address,
            'subject': self.subject,
            'body': self.body,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> 'PreparedMessage':
        try:
            return cls(
                task_id=str(raw['task_id']),
                idempotency_key=str(raw['idempotency_key']),
                subscriber_id=str(raw['subscriber_id']),
                address=str(raw['address']),
                subject=str(raw.get('subject', '')),
                body=str(raw.get('body', '')),
            )
        except (KeyError, TypeError) as exc:
            raise EnvelopeError(f'Письмо описано неполно: {exc}') from exc


def build_campaign_launched(*, run_id: str, campaign_id: str) -> dict[str, Any]:
    """Событие «рассылка запущена» — просьба развернуть её в получателей.

    Список получателей здесь не передаётся СПЕЦИАЛЬНО: собирать его внутри
    HTTP-запроса админки значит держать менеджера на странице всё время выборки,
    а теория отдельно предупреждает, что сбор данных в источнике события тормозит
    систему.
    """
    return {
        **_head(EVENT_CAMPAIGN_LAUNCHED, run_id),
        'notification_id': campaign_id,
    }


def build_notification_requested(
    *,
    run_id: str,
    campaign_id: str,
    channel: str,
    category: str,
    content_id: str,
    template_code: str,
    template_revision: int,
    context: dict[str, Any],
    valid_until: datetime,
    targets: list[TargetRef],
) -> dict[str, Any]:
    """Пачка идентификаторов, которую предстоит превратить в письма.

    ``template`` и ``context`` здесь есть, а личных данных нет: шаблон и общий
    контекст рассылки известны заранее, а имя, фамилия и адрес — нет, за ними
    формирующий воркер сходит в Auth.
    """
    return {
        **_head(EVENT_NOTIFICATION_REQUESTED, run_id),
        'notification_id': campaign_id,
        'content_id': content_id,
        'channel': channel,
        'category': category,
        'template': {'code': template_code, 'revision': template_revision},
        'context': context,
        # Срок годности события. Уведомление о новой серии, доехавшее до воркера
        # через сутки, пользователю уже не нужно — теория предлагает дать воркеру
        # право решать, отправлять его или нет.
        'valid_until': valid_until.astimezone(UTC).isoformat(),
        'targets': [target.as_dict() for target in targets],
    }


def build_notification_prepared(
    *,
    run_id: str,
    campaign_id: str,
    channel: str,
    category: str,
    content_id: str,
    valid_until: datetime | None,
    recipients: list[PreparedMessage],
) -> dict[str, Any]:
    """Пачка собранных писем, готовых к отправке.

    ``template`` и ``context`` не переносятся: рендер уже произошёл, и его
    входные данные отправляющему воркеру не нужны. ``valid_until`` переносится —
    письмо могло пролежать в очереди отправки дольше, чем событие остаётся
    актуальным, и последнее слово о свежести остаётся за отправкой.
    """
    return {
        **_head(EVENT_NOTIFICATION_PREPARED, run_id),
        'notification_id': campaign_id,
        'content_id': content_id,
        'channel': channel,
        'category': category,
        'valid_until': valid_until.astimezone(UTC).isoformat() if valid_until else None,
        'recipients': [recipient.as_dict() for recipient in recipients],
    }


def build_delivery_report(
    *, run_id: str, campaign_id: str, delivered: int, failed: int, skipped: int
) -> dict[str, Any]:
    """Отчётное событие: что стало с пачкой. Никто не обязан его слушать."""
    event_type = EVENT_NOTIFICATION_DELIVERED if failed == 0 else EVENT_NOTIFICATION_FAILED
    return {
        **_head(event_type, run_id),
        'notification_id': campaign_id,
        'delivered': delivered,
        'failed': failed,
        'skipped': skipped,
    }


def parse(raw: dict[str, Any]) -> dict[str, Any]:
    """Проверить обязательные поля и версию. Лишние ключи проходят как есть."""
    if not isinstance(raw, dict):
        raise EnvelopeError('Тело сообщения должно быть объектом JSON')

    missing = [name for name in REQUIRED_FIELDS if name not in raw]
    if missing:
        raise EnvelopeError(f'В конверте нет обязательных полей: {", ".join(missing)}')

    version = raw['schema_version']
    if not isinstance(version, int) or version > SCHEMA_VERSION:
        raise EnvelopeError(f'Неизвестная версия конверта: {version!r} (поддерживается до {SCHEMA_VERSION})')

    return raw


def parse_targets(raw: dict[str, Any]) -> list[TargetRef]:
    return [TargetRef.from_dict(item) for item in raw.get('targets') or []]


def parse_recipients(raw: dict[str, Any]) -> list[PreparedMessage]:
    return [PreparedMessage.from_dict(item) for item in raw.get('recipients') or []]


def parse_valid_until(raw: dict[str, Any]) -> datetime | None:
    value = raw.get('valid_until')
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except (TypeError, ValueError) as exc:
        raise EnvelopeError(f'Некорректный valid_until: {value!r}') from exc


def _head(event_type: str, run_id: str) -> dict[str, Any]:
    return {
        'schema_version': SCHEMA_VERSION,
        'event_id': str(uuid.uuid4()),
        'occurred_at': datetime.now(UTC).isoformat(),
        'type': event_type,
        'run_id': run_id,
    }
