"""Приём заявок извне: фиксированные события и прямые сообщения.

Ручка приёма — центральный узел, и рассылкой она не занимается: всё, что здесь
происходит, — это создание прогона и строки в outbox в одной транзакции. Ни
выборки получателей, ни публикации в брокер, ни SMTP внутри HTTP-запроса нет,
потому что теория предупреждает об этом прямо: сбор данных в источнике события
тормозит систему, которая событие породила.

## Почему коды ответа — часть контракта

Продюсер повторяет запрос при ошибке, поэтому «ничего не произошло» обязано
отличаться от «попробуй ещё раз». Отсюда три исхода с кодом 200
(``duplicate``/``ignored``): на выключенной привязке продюсер, который ретраит
всё не-2xx, крутился бы вечно. Ретрай осмыслен только на 5xx и на обрыве связи.

## Почему категорию нельзя передать в запросе

Категория решает, действует ли отписка пользователя: ``transactional`` не
отключается вовсе. Вызывающий, который может поставить категорию сам, может
пройти мимо любой отписки — поэтому категория берётся из рассылки, на которую
указывает привязка, и меняется только менеджером в панели.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from django.db import IntegrityError

from practix_notifications.campaigns.models import Campaign, EventBinding
from practix_notifications.content.models import DEFAULT_ALLOWED_VARIABLES, DEFAULT_SAMPLE_CONTEXT, MessageTemplate
from practix_notifications.core.config import settings
from practix_notifications.domain_events import context_from, dedup_key, missing_fields
from practix_notifications.enums import BodyFormat, CampaignStatus, Channel, DomainEvent, EventAudience
from practix_notifications.services.launch import LaunchError, RunSpec, launch_for_event, launch_now
from practix_notifications.services.templating import TemplateValidationError, validate_template
from practix_notifications.subscribers.models import Subscriber

logger = logging.getLogger('notifications.intake')


@dataclass(frozen=True)
class IntakeResult:
    """Что вернуть вызывающему. Вью только сериализует это в ``JsonResponse``."""

    status: int
    body: dict[str, Any] = field(default_factory=dict)


def _queued(run) -> IntakeResult:
    return IntakeResult(202, {'status': 'queued', 'run_id': str(run.id), 'event_type': run.event_type})


def _duplicate(run) -> IntakeResult:
    return IntakeResult(200, {'status': 'duplicate', 'run_id': str(run.id)})


def _ignored(reason: str) -> IntakeResult:
    """Заявка принята и осознанно не привела ни к чему. Повтор не поможет."""
    return IntakeResult(200, {'status': 'ignored', 'reason': reason})


def _rejected(detail: str, *, status: int = 400, **extra: Any) -> IntakeResult:
    return IntakeResult(status, {'detail': detail, **extra})


def handle_campaign_launch(payload: dict[str, Any]) -> IntakeResult:
    """Запуск СУЩЕСТВУЮЩЕЙ рассылки по идентификатору.

    Исходный контракт ручки, сохранённый дословно: этой формой пользуются
    планировщики и скрипты, которые уже написаны против неё.
    """
    campaign = _get_campaign(payload.get('campaign_id'))
    if campaign is None:
        return _rejected('campaign not found', status=404)
    if campaign.status == CampaignStatus.CANCELLED.value:
        return _rejected('campaign is cancelled', status=409)

    try:
        run = launch_now(campaign)
    except LaunchError as exc:
        return _rejected(str(exc), status=409)

    if run is None:
        # Повтор в пределах секунды — не ошибка вызывающего: он получает ссылку
        # на то, что уже запущено, и может считать запрос выполненным.
        return IntakeResult(200, {'status': 'duplicate'})
    return IntakeResult(202, {'status': 'queued', 'run_id': str(run.id)})


def handle_domain_event(payload: dict[str, Any]) -> IntakeResult:
    """Фиксированное событие проекта: регистрация, новый фильм и так далее."""
    event_type = payload.get('type', '')
    if event_type not in {member.value for member in DomainEvent}:
        return _rejected(f'unknown event type: {event_type!r}')
    if event_type == DomainEvent.NOTIFICATION_DIRECT.value:
        # Прямое сообщение приходит своей ручкой: там `type` означает канал, а не
        # имя события, и смешивать два смысла одного поля нельзя.
        return _rejected('use POST /api/v1/notifications/messages for direct messages')

    data = payload.get('data')
    if not isinstance(data, dict):
        return _rejected('data must be an object')
    missing = missing_fields(event_type, data)
    if missing:
        return _rejected(f'missing required fields: {", ".join(missing)}')

    event_id = str(payload.get('event_id') or uuid.uuid4())
    occurred_at, error = _parse_occurred_at(payload.get('occurred_at'))
    if error:
        return _rejected(error)

    binding, problem = _resolve_binding(event_type)
    if problem is not None:
        return problem

    spec_kwargs: dict[str, Any] = {
        'context_overrides': context_from(event_type, data),
        'event_type': event_type,
        'event_id': event_id,
    }

    if binding.audience == EventAudience.SUBJECT.value:
        subscriber, problem = _resolve_subject(binding, data, occurred_at)
        if problem is not None:
            return problem
        spec_kwargs['audience_subscriber_id'] = str(subscriber.id)

    return _launch(binding, key=dedup_key(event_type, data, event_id), spec=RunSpec(**spec_kwargs))


def handle_direct_message(payload: dict[str, Any]) -> IntakeResult:
    """Сообщение в свободном формате: ``user_id``, ``template_id``/текст, канал.

    Отдельная ручка, а не ветка приёма событий, потому что здесь ``type``
    означает КАНАЛ доставки, а там — имя события. Одно поле с двумя смыслами на
    одном URL — ловушка для того, кто будет писать третьего продюсера.
    """
    event_id = payload.get('event_id')
    if not event_id:
        # Природного ключа у прямого сообщения нет: два одинаковых напоминания —
        # легитимный сценарий, и хеш тела молча склеил бы их в одно. Ключ
        # дедупликации обязан принести вызывающий.
        return _rejected('event_id is required: it is the deduplication key')

    user_id = payload.get('user_id')
    if not user_id:
        return _rejected('user_id is required')

    channel = payload.get('channel') or payload.get('type') or Channel.EMAIL.value
    if channel not in {member.value for member in Channel}:
        return _rejected(f'unknown channel: {channel!r}')

    # Содержимое проверяется ДО привязки и получателя: ошибки вызывающего
    # (несуществующий шаблон, сломанный текст, взаимоисключающие поля) обязаны
    # приходить к нему кодом 4xx. Если сначала спросить привязку, тот же кривой
    # запрос на не настроенном стенде получил бы 200 `ignored` и выглядел бы
    # принятым.
    content, problem = _resolve_content(payload)
    if problem is not None:
        return problem

    binding, problem = _resolve_binding(DomainEvent.NOTIFICATION_DIRECT.value)
    if problem is not None:
        return problem

    subscriber = _get_subscriber(user_id)
    if subscriber is None:
        return _ignored('unknown_subscriber')

    spec = RunSpec(
        audience_subscriber_id=str(subscriber.id),
        context_overrides=dict(payload.get('context') or {}),
        channel_override=channel,
        event_type=DomainEvent.NOTIFICATION_DIRECT.value,
        event_id=str(event_id),
        **content,
    )
    return _launch(binding, key=str(event_id), spec=spec)


def _launch(binding: EventBinding, *, key: str, spec: RunSpec) -> IntakeResult:
    try:
        run, created = launch_for_event(binding, key=key, spec=spec)
    except LaunchError as exc:
        # Выключенный шаблон — состояние, которое чинит менеджер, а не повтор.
        return _rejected(str(exc), status=409)
    return _queued(run) if created else _duplicate(run)


def _resolve_binding(event_type: str) -> tuple[EventBinding | None, IntakeResult | None]:
    binding = (
        EventBinding.objects.filter(event_type=event_type).select_related('campaign', 'campaign__template').first()
    )
    if binding is None:
        # Событие описано в словаре, но менеджер не сказал, чем на него отвечать.
        # Это не ошибка продюсера — и уж точно не повод его ретраить.
        logger.info('No binding for event %s, ignoring', event_type)
        return None, _ignored('no_binding')
    if not binding.is_enabled:
        logger.info('Binding for event %s is disabled, ignoring', event_type)
        return None, _ignored('binding_disabled')
    return binding, None


def _resolve_subject(
    binding: EventBinding, data: dict[str, Any], occurred_at: datetime | None
) -> tuple[Subscriber | None, IntakeResult | None]:
    """Найти или завести получателя адресного письма."""
    user_id = data.get('user_id')
    if not user_id:
        return None, _rejected('user_id is required for a subject-addressed event')

    if binding.upsert_subscriber:
        try:
            subscriber = _upsert_subscriber(data, occurred_at)
        except IntegrityError:
            # Уникальность email. Адрес переиспользован (старого пользователя в
            # Auth удалили, а витрина подписчиков удалений не знает). Повтор
            # запроса это не вылечит, поэтому 200, а не 5xx, — но в лог ошибкой.
            logger.error('Email conflict while upserting subscriber %s', user_id, exc_info=True)
            return None, _ignored('email_conflict')
        return subscriber, None

    subscriber = _get_subscriber(user_id)
    if subscriber is None:
        # Витрина ещё не синхронизирована, а заводить подписчика эта привязка не
        # уполномочена: события с чужими контактами не должны создавать людей.
        return None, _ignored('unknown_subscriber')
    return subscriber, None


def _upsert_subscriber(data: dict[str, Any], occurred_at: datetime | None) -> Subscriber:
    """Завести или обновить подписчика из данных события.

    Точное совпадение возможно потому, что ``Subscriber.id`` и есть ``user.id`` в
    Auth: угадывать по email не приходится.
    """
    now = datetime.now(UTC)
    contact = {
        'login': data.get('login') or '',
        'email': data['email'],
        'is_active': True,
        'source_version': occurred_at,
        'synced_at': now,
    }
    subscriber, _ = Subscriber.objects.update_or_create(
        pk=data['user_id'],
        # При наличии create_defaults Django применяет defaults ТОЛЬКО к
        # обновлению, поэтому здесь остаются лишь поля контакта: таймзону и язык
        # человек мог поменять у себя, и повторное событие о регистрации не
        # должно их перетирать.
        defaults=contact,
        # А при создании нужны и они — иначе подписчик заведётся без часового
        # пояса, и тихие часы посчитаются не в его времени.
        create_defaults={
            **contact,
            'timezone': data.get('timezone') or settings.NOTIFY_DEFAULT_TIMEZONE,
            'locale': data.get('locale') or 'ru',
        },
    )
    return subscriber


def _get_subscriber(user_id: Any) -> Subscriber | None:
    try:
        return Subscriber.objects.filter(pk=uuid.UUID(str(user_id))).first()
    except ValueError:
        # Невалидный UUID — в фильтр его отдавать нельзя: Postgres ответит
        # DataError, то есть пятисоткой на ошибку вызывающего.
        return None


def _get_campaign(campaign_id: Any) -> Campaign | None:
    """То же прикрытие, что и у подписчика, и по той же причине.

    ``filter(pk='abc')`` по ``UUIDField`` — это не пустая выборка, а исключение
    на уровне поля, то есть 500 в ответ на опечатку вызывающего.
    """
    try:
        key = uuid.UUID(str(campaign_id))
    except ValueError:
        return None
    return Campaign.objects.filter(pk=key).select_related('template').first()


def _resolve_content(payload: dict[str, Any]) -> tuple[dict[str, Any], IntakeResult | None]:
    """Текст прямого сообщения: из шаблона или сырой, но всегда проверенный."""
    template_id = payload.get('template_id')
    subject = payload.get('subject')
    body = payload.get('text')

    if template_id and (subject or body):
        return {}, _rejected('template_id and subject/text are mutually exclusive')
    if template_id:
        template = _find_template(template_id)
        if template is None:
            return {}, _rejected(f'unknown template: {template_id!r}', status=404)
        return {
            'subject': template.subject_template,
            'body': template.body_template,
            'is_html': template.is_html,
            'template_revision': template.revision,
        }, None

    if not body:
        return {}, _rejected('either template_id or text is required')

    # Формат по умолчанию текстовый: поле в контракте называется `text`, а
    # autoescape на плоском теле положил бы в письмо `&quot;` вместо кавычек.
    is_html = (payload.get('format') or BodyFormat.TEXT.value) == BodyFormat.HTML.value
    if not settings.NOTIFY_FREEFORM_RAW_TEXT_ENABLED:
        return {}, _rejected('raw text is disabled, use template_id', status=409)

    problem = _validate_raw(subject or '', body, is_html=is_html, context=payload.get('context') or {})
    if problem is not None:
        return {}, problem
    return {'subject': subject or '', 'body': body, 'is_html': is_html, 'template_revision': 0}, None


def _find_template(template_id: Any) -> MessageTemplate | None:
    """Шаблон по коду или по идентификатору — вызывающему удобно и то и другое."""
    queryset = MessageTemplate.objects.filter(is_active=True)
    template = queryset.filter(code=str(template_id)).first()
    if template is not None:
        return template
    try:
        return queryset.filter(pk=uuid.UUID(str(template_id))).first()
    except ValueError:
        return None


def _validate_raw(subject: str, body: str, *, is_html: bool, context: dict[str, Any]) -> IntakeResult | None:
    """Сырой текст — это шаблон, и проверяется он теми же тремя проверками.

    Синхронно на приёме, а не в воркере: вызывающий — сервис, и узнать о
    сломанном шаблоне он обязан из кода ответа, а не из dead-letters через три
    перехода. Белый список — переменные шаблонизатора плюс те, что вызывающий
    принёс сам: свои переменные можно, дотянуться до чего-то ещё — нельзя.
    """
    allowed = set(DEFAULT_ALLOWED_VARIABLES) | set(context)
    sample = {**DEFAULT_SAMPLE_CONTEXT, **context}
    for source, source_is_html in ((subject, False), (body, is_html)):
        if not source:
            continue
        try:
            validate_template(
                source,
                allowed=allowed,
                sample=sample,
                is_html=source_is_html,
                max_bytes=settings.NOTIFY_TEMPLATE_MAX_BYTES,
                timeout=settings.NOTIFY_RENDER_TIMEOUT_SECONDS,
            )
        except TemplateValidationError as exc:
            return _rejected('template is invalid', errors=list(exc.errors))
    return None


def _parse_occurred_at(raw: Any) -> tuple[datetime | None, str | None]:
    if not raw:
        return None, None
    try:
        moment = datetime.fromisoformat(str(raw))
    except ValueError:
        return None, 'occurred_at must be an ISO-8601 timestamp'
    # Наивное время из чужого сервиса считаем UTC: витрина подписчиков хранит
    # source_version только для сравнения «свежее/старее», и сдвиг на локальную
    # зону процесса сделал бы это сравнение неверным.
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)), None
