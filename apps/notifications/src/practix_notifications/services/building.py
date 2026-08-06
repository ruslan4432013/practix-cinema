"""Формирующий воркер: из идентификаторов — в готовое письмо.

Забирает пачку из ``notifications.build-email``, где лежат ТОЛЬКО
идентификаторы, сам идёт в Auth за именем, фамилией и адресом, собирает текст по
снапшоту прогона и публикует готовые письма в ``notifications.send-email``.

Это прямая реализация вывода теории («Отправка уведомления»): «Лучший вариант —
собирать данные воркером в неспешном режиме. Отправить быстрый запрос в API
системы уведомлений, которая так же быстро положит его в очередь». Данные о
событии — не данные для уведомления: событием может быть новая серия, а для
письма нужны ещё адрес и имя, к событию не относящиеся.

## Что здесь решается, а что оставлено отправке

Здесь — то, после чего письма гарантированно не будет: событие протухло, канал
не реализован, человека нет в Auth, у него нет адреса, шаблон не собрался.
Отписки и активность СЮДА НЕ ПЕРЕЕХАЛИ: они проверяются в момент отправки, и это
обещание сервиса — между сборкой и отправкой человек ещё может отписаться.

## Отказ Auth

Ни одного письма наполовину. Если Auth недоступен, пачка целиком возвращается в
парковочную очередь и ждёт: письмо без имени хуже, чем письмо на десять минут
позже, а обезличивать его по устаревшей локальной копии — значит тихо подменить
то, ради чего этот воркер и существует. Ровно этого требует failover-пункт
чек-листа: «при отказе одной из подсистем нужно иметь возможность мгновенно
отключить любые запросы на неё».
"""

import logging
from datetime import UTC, datetime

from django.db import transaction
from jinja2 import TemplateError

from practix_notifications.broker import topology
from practix_notifications.broker.consumer import Outcome
from practix_notifications.broker.envelope import (
    PreparedMessage,
    build_notification_prepared,
    parse_targets,
    parse_valid_until,
)
from practix_notifications.broker.publisher import BrokerConnection, PublishFailed
from practix_notifications.campaigns.models import DeliveryTask, ScheduledRun
from practix_notifications.channels import is_implemented
from practix_notifications.core.config import settings
from practix_notifications.enums import AttemptResult, Channel, SkipReason, TaskStatus
from practix_notifications.services import journal, templating
from practix_notifications.services.auth_client import AuthClientError, AuthUnavailable
from practix_notifications.services.confirm_links import confirm_url
from practix_notifications.services.directory import Directory, Person, template_vars
from practix_notifications.services.journal import BatchResult
from practix_notifications.services.links import unsubscribe_url
from practix_notifications.services.shortener_client import (
    ShortenerClient,
    ShortenerClientError,
    ShortenerUnavailable,
)

logger = logging.getLogger('notifications.building')

#: Имя переменной, наличие которой в снапшоте прогона включает поход в сервис
#: сокращения ссылок. Одна строка, потому что упоминается в двух местах: здесь и
#: в белом списке переменных шаблона (``content/models.py``).
CONFIRM_URL_VARIABLE = 'confirm_url'


def handle_notification_requested(broker: BrokerConnection, directory: Directory, body: dict, attempt: int) -> Outcome:
    """Обработчик очереди ``notifications.build-email``."""
    run = ScheduledRun.objects.filter(pk=body.get('run_id')).select_related('campaign').first()
    if run is None:
        logger.error('Run %s not found, dead-lettering batch', body.get('run_id'))
        return Outcome.DEAD

    targets = parse_targets(body)
    valid_until = parse_valid_until(body)
    context = dict(body.get('context') or {})
    result = BatchResult()

    # 1. Отсев ДО обращения к Auth. Переотправленная пачка (воркер упал между
    #    публикацией и ack) не должна устраивать второй шторм запросов за уже
    #    отработанными получателями.
    buildable = _prune(targets, valid_until=valid_until, result=result)

    # 2. Личности пачкой. Отказ Auth — повод подождать всей пачкой, а не
    #    отправить часть писем без имени.
    try:
        # str(), а не UUID: идентификатор уезжает в JSON-тело запроса к Auth и
        # ключом словаря сравнивается с тем, что вернулось строкой.
        people = directory.resolve([str(task.subscriber_id) for task in buildable]) if buildable else {}
    except AuthUnavailable as exc:
        logger.warning('Auth unavailable, retrying whole batch: %s', exc)
        # Терминальные исходы шага 1 уже записаны и идемпотентны: повтор пройдёт
        # мимо них, а не отменит их.
        journal.apply_counters(run, result)
        return Outcome.RETRY
    except AuthClientError:
        # Не тот пароль сервисной учётки, отобранная роль, исчезнувшая ручка.
        # Повтор это не лечит — пачке место в dead-letters, под глаза дежурному.
        logger.exception('Auth rejected the request, dead-lettering batch')
        journal.apply_counters(run, result)
        return Outcome.DEAD

    # 3. Ссылки подтверждения — только если шаблон их просит. Выпускать их
    #    безусловно значило бы заводить строку в сервисе ссылок на КАЖДОГО
    #    получателя ЛЮБОЙ массовой рассылки.
    confirm_urls: dict[str, str] = {}
    if buildable and _wants_confirm_url(run):
        try:
            confirm_urls = _mint_confirm_urls(buildable, redirect_url=str(context.get('redirect_url') or ''))
        except ShortenerUnavailable as exc:
            # Та же логика, что и с недоступным Auth: письмо с мёртвой ссылкой
            # подтверждения хуже, чем письмо на десять минут позже.
            logger.warning('Shortener unavailable, retrying whole batch: %s', exc)
            journal.apply_counters(run, result)
            return Outcome.RETRY
        except ShortenerClientError:
            # Не тот токен, целевой адрес вне белого списка. Повтор не лечит.
            logger.exception('Shortener rejected the request, dead-lettering batch')
            journal.apply_counters(run, result)
            return Outcome.DEAD

    # 4. Сборка писем.
    prepared: list[PreparedMessage] = []
    resolved_tasks: list[DeliveryTask] = []
    for task in buildable:
        message = _build_one(run, task, people, context=context, confirm_urls=confirm_urls, result=result)
        if message is None:
            continue
        prepared.append(message)
        resolved_tasks.append(task)

    # 5. Адрес на задаче — то, по чему потом видно, куда ушло письмо; на нём же
    #    держится проверка `no_address` в отправляющем воркере.
    if resolved_tasks:
        DeliveryTask.objects.bulk_update(resolved_tasks, ['address', 'updated_at'])

    # 6. Публикация. Пачки МЕНЬШЕ, чем у веера: каждое сообщение теперь везёт
    #    готовые тела писем.
    try:
        _publish(broker, run, body, prepared)
    except PublishFailed:
        logger.exception('Could not publish prepared messages')
        journal.apply_counters(run, result)
        return Outcome.RETRY

    # 7. Счётчики за СВОИ исходы. Без этого пачка, отсеянная целиком, не закрыла
    #    бы прогон и кампания навсегда осталась бы «идущей».
    journal.apply_counters(run, result)
    logger.info(
        'Build done',
        extra={'run_id': str(run.id), 'prepared': len(prepared), 'skipped': result.skipped, 'failed': result.failed},
    )
    return Outcome.ACK


def _prune(targets, *, valid_until: datetime | None, result: BatchResult) -> list[DeliveryTask]:
    """Отсеять всё, ради чего не стоит беспокоить Auth."""
    if not targets:
        return []

    by_id = {
        str(task.id): task
        for task in DeliveryTask.objects.filter(pk__in=[target.task_id for target in targets]).select_related(
            'subscriber'
        )
    }
    now = datetime.now(UTC)
    buildable: list[DeliveryTask] = []

    for target in targets:
        task = by_id.get(target.task_id)
        if task is None:
            logger.warning('Delivery task %s vanished, ignoring target', target.task_id)
            continue
        if task.status in (TaskStatus.SENT.value, TaskStatus.SKIPPED.value, TaskStatus.FAILED.value):
            # Уже отработана. Молча мимо — это и есть идемпотентность сборки.
            continue

        started_at = datetime.now(UTC)
        if valid_until is not None and now > valid_until:
            _terminate(task, SkipReason.STALE_EVENT, 'valid_until passed', started_at, result)
            continue
        if not is_implemented(task.channel):
            # Собирать письмо для канала, который некому отправить, незачем.
            # `_skip_reason` в отправляющем воркере остаётся второй линией обороны.
            _terminate(task, SkipReason.CHANNEL_UNAVAILABLE, 'channel not implemented', started_at, result)
            continue
        buildable.append(task)

    return buildable


def _wants_confirm_url(run: ScheduledRun) -> bool:
    """Просит ли снапшот прогона переменную ``confirm_url``.

    Смотрим В СНАПШОТ, а не в шаблон кампании: текст рассылки зафиксирован на
    прогоне, и правка шаблона посреди веера не должна менять то, что мы делаем
    для уже начатой рассылки.
    """
    try:
        wanted = templating.find_variables(run.body_snapshot, is_html=run.is_html)
        wanted |= templating.find_variables(run.subject_snapshot, is_html=False)
    except TemplateError:
        # Неразбираемый шаблон НЕ должен ронять пачку здесь. Он всё равно упадёт
        # на рендере — но там отказ персональный (`_build_one` помечает FAILED
        # одного получателя), а отсюда он утащил бы за собой всю пачку, включая
        # тех, чьи письма собрались бы нормально.
        logger.warning('Run %s: не удалось разобрать снапшот, ссылки подтверждения не выпускаются', run.id)
        return False
    return CONFIRM_URL_VARIABLE in wanted


def _mint_confirm_urls(tasks: list[DeliveryTask], *, redirect_url: str) -> dict[str, str]:
    """Ссылка подтверждения на каждого получателя пачки: ``{task_id: url}``.

    Ключ идемпотентности — ``task.idempotency_key`` (уже
    ``sha256(run:subscriber:channel)``): пересобранная пачка получит те же самые
    ссылки, а не вторую пачку строк в шортенере.
    """
    client = ShortenerClient()
    return {
        str(task.id): confirm_url(
            client,
            user_id=str(task.subscriber_id),
            redirect_url=redirect_url,
            idempotency_key=task.idempotency_key,
        )
        for task in tasks
    }


def _build_one(
    run: ScheduledRun,
    task: DeliveryTask,
    people: dict[str, Person],
    *,
    context: dict,
    confirm_urls: dict[str, str],
    result: BatchResult,
) -> PreparedMessage | None:
    started_at = datetime.now(UTC)
    person = people.get(str(task.subscriber_id))

    if person is None:
        _terminate(task, SkipReason.UNKNOWN_USER, 'user not found in Auth', started_at, result)
        return None

    # Адрес зависит от канала. У websocket-получателя почтового адреса нет
    # вовсе — он адресуется тем же UUID, что и в Auth, и требовать от него email
    # значило бы не пустить в кабинет человека, который просто не оставлял почту.
    # Auth при этом опрашивается и для websocket: имя и фамилия нужны шаблону
    # ровно так же, а «пользователя нет в Auth» остаётся отдельной причиной.
    address = str(task.subscriber_id) if task.channel == Channel.WEBSOCKET.value else person.email
    if not address:
        _terminate(task, SkipReason.NO_ADDRESS, 'Auth has no email for user', started_at, result)
        return None

    # Порядок победы: переменные получателя > контекст события > контекст
    # рассылки. Персональное всегда сильнее общего.
    merged = {
        **context,
        **template_vars(
            person,
            unsubscribe_url=unsubscribe_url(str(task.subscriber_id), task.channel, ''),
            confirm_url=confirm_urls.get(str(task.id), ''),
        ),
    }
    try:
        # Текст берётся из СНАПШОТА прогона, а не из текущего шаблона: правка
        # шаблона посреди рассылки не должна расщеплять её на «до» и «после».
        subject = templating.render(run.subject_snapshot, merged, is_html=False)
        rendered_body = templating.render(run.body_snapshot, merged, is_html=run.is_html)
    except TemplateError as exc:
        logger.exception('Template render failed for task %s', task.id)
        with transaction.atomic():
            journal.finish(task, TaskStatus.FAILED, error=f'template render failed: {exc}')
            result.failed += 1
            journal.record(task, AttemptResult.FAILED, started_at, error='template render failed')
        return None

    task.address = address
    return PreparedMessage(
        task_id=str(task.id),
        idempotency_key=task.idempotency_key,
        subscriber_id=str(task.subscriber_id),
        address=address,
        subject=subject,
        body=rendered_body,
    )


def _terminate(task: DeliveryTask, reason: SkipReason, error: str, started_at: datetime, result: BatchResult) -> None:
    with transaction.atomic():
        journal.finish(task, TaskStatus.SKIPPED, skip_reason=reason, error=error)
        result.skipped += 1
        journal.record(task, AttemptResult.SKIPPED, started_at, error=error)


def _publish(broker: BrokerConnection, run: ScheduledRun, body: dict, prepared: list[PreparedMessage]) -> None:
    batch = settings.NOTIFY_BUILD_MESSAGE_BATCH
    for index in range(0, len(prepared), batch):
        chunk = prepared[index : index + batch]
        broker.publish(
            exchange=topology.EXCHANGE_EVENTS,
            routing_key=topology.RK_NOTIFICATION_PREPARED,
            body=build_notification_prepared(
                run_id=str(run.id),
                campaign_id=str(run.campaign_id),
                channel=body.get('channel') or '',
                category=body.get('category') or '',
                content_id=body.get('content_id') or '',
                valid_until=parse_valid_until(body),
                recipients=chunk,
            ),
            # Заголовки НЕ переносятся: счётчик попыток сборки не должен
            # съедать бюджет попыток отправки. Это разные отказы и разные
            # подсистемы.
            headers={},
        )
