"""Запуск рассылки: создание прогона и постановка события в outbox.

Единственная точка, через которую рассылка вообще может начаться, — и для кнопки
«Запустить сейчас» в админке, и для тика планировщика, и для приёма события
извне. Сценарии из теории различаются ровно тем, КТО сюда пришёл; всё, что после,
у них общее.

Внутри одной транзакции происходит три вещи и ни одной больше:

1. фиксируется прогон со СНАПШОТОМ текста;
2. в outbox кладётся событие «рассылка запущена»;
3. рассылка получает статус «в очереди».

Чего здесь НЕТ намеренно: разворачивания сегмента в получателей. Собрать список
адресов внутри HTTP-запроса админки — это держать менеджера на странице всё
время выборки и попутно нагрузить базу; теория предупреждает об этом отдельно.
Веер делает отдельный обработчик, не спеша.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from django.db import IntegrityError, transaction

from practix_notifications.broker import topology
from practix_notifications.broker.envelope import build_campaign_launched
from practix_notifications.campaigns.models import Campaign, CampaignSchedule, EventBinding, ScheduledRun
from practix_notifications.domain_events import event_run_key
from practix_notifications.enums import CampaignStatus, RunStatus
from practix_notifications.services import outbox
from practix_notifications.services.scheduling import manual_run_key

logger = logging.getLogger('notifications.launch')


class LaunchError(Exception):
    pass


@dataclass(frozen=True)
class RunSpec:
    """Чем прогон отличается от «вся аудитория рассылки, текст из шаблона».

    Пустой ``RunSpec`` — это ровно сегодняшнее поведение, поэтому кнопка в
    админке и тик планировщика его не передают вовсе. Всё, что здесь есть,
    приносит событие извне.
    """

    #: Заполнено — прогон адресный: ровно один получатель, статус рассылки не двигается.
    audience_subscriber_id: str | None = None
    #: Переменные события. Побеждают контекст рассылки, проигрывают персональным.
    context_overrides: dict[str, Any] = field(default_factory=dict)
    channel_override: str = ''
    event_type: str = ''
    event_id: str = ''
    #: Текст мимо шаблона рассылки — для свободного формата и для прямого
    #: сообщения по другому шаблону. ``None`` = взять из шаблона рассылки.
    subject: str | None = None
    body: str | None = None
    is_html: bool | None = None
    #: Ревизия того шаблона, чей текст лёг в снапшот. Поле информационное (рендер
    #: идёт из снапшота), но врать в журнале ему незачем.
    template_revision: int | None = None


_EMPTY_SPEC = RunSpec()


def launch(
    campaign: Campaign,
    *,
    run_key: str,
    planned_for: datetime,
    schedule: CampaignSchedule | None = None,
    spec: RunSpec | None = None,
) -> ScheduledRun | None:
    """Создать прогон и поставить событие в outbox.

    :returns: созданный прогон или ``None``, если прогон с таким ключом уже есть.
        ``None`` — это НЕ ошибка: именно так выглядит защита от повторного
        срабатывания того же слота, от двойного клика по кнопке и от повтора
        запроса продюсером события.
    """
    spec = spec or _EMPTY_SPEC
    template = campaign.template
    if not template.is_active:
        raise LaunchError(f'Шаблон «{template.name}» выключен — рассылка не может быть запущена')

    is_html = template.is_html if spec.is_html is None else spec.is_html
    try:
        with transaction.atomic():
            run = ScheduledRun.objects.create(
                schedule=schedule,
                campaign=campaign,
                run_key=run_key,
                planned_for=planned_for,
                status=RunStatus.PLANNED.value,
                subject_snapshot=template.subject_template if spec.subject is None else spec.subject,
                body_snapshot=template.body_template if spec.body is None else spec.body,
                template_revision=template.revision if spec.template_revision is None else spec.template_revision,
                is_html=is_html,
                audience_subscriber_id=spec.audience_subscriber_id,
                context_overrides=spec.context_overrides,
                channel_override=spec.channel_override,
                event_type=spec.event_type,
                event_id=spec.event_id,
            )
            outbox.enqueue(
                exchange=topology.EXCHANGE_EVENTS,
                routing_key=topology.RK_CAMPAIGN_LAUNCHED,
                body=build_campaign_launched(run_id=str(run.id), campaign_id=str(campaign.id)),
            )
            if not run.is_targeted:
                # Письмо одному человеку — это не «рассылка идёт». Статус,
                # сдвинутый адресным прогоном, (а) врал бы в списке рассылок и
                # (б) взводил бы защиту launch_now против самого менеджера.
                Campaign.objects.filter(pk=campaign.pk).update(status=CampaignStatus.QUEUED.value)
    except IntegrityError:
        # Уникальность run_key. Второй тик того же слота или второй клик по
        # кнопке — штатная ситуация, а не повод шуметь в лог ошибкой.
        logger.info('Run %s already exists, skipping duplicate launch', run_key)
        return None

    logger.info(
        'Campaign launched',
        extra={'campaign_id': str(campaign.id), 'run_id': str(run.id), 'run_key': run_key},
    )
    return run


def launch_for_event(
    binding: EventBinding, *, key: str, spec: RunSpec, at: datetime | None = None
) -> tuple[ScheduledRun, bool]:
    """Запуск по фиксированному событию.

    Зовёт ``launch`` напрямую, а НЕ ``launch_now``: у того есть защита «рассылка
    уже в очереди или отправляется», и триггерная рассылка при сотне регистраций
    в минуту почти всегда находится в этом состоянии — события со второго по
    сотое молча вернули бы «дубликат» и не отправились бы никогда. Повторы здесь
    ловит уникальность ``run_key``, и только она.

    :returns: пара «прогон, создан ли он сейчас». ``False`` означает, что
        продюсер повторил запрос, — и вызывающему нужно вернуть 200 «дубликат», а
        не создавать ничего второй раз.
    """
    moment = at or datetime.now(UTC)
    run_key = event_run_key(binding.event_type, key)
    run = launch(binding.campaign, run_key=run_key, planned_for=moment, spec=spec)
    if run is not None:
        return run, True
    # Прогон с таким ключом уже есть: это ретрай продюсера, а не ошибка.
    return ScheduledRun.objects.get(run_key=run_key), False


def launch_now(campaign: Campaign, *, at: datetime | None = None) -> ScheduledRun | None:
    """«Запустить сейчас» из админки или из ручки запуска существующей рассылки.

    Две линии защиты от повторного запуска, и они закрывают разные случаи:

    * идущая рассылка (статус «в очереди» или «отправляется») не запускается
      второй раз — это защита от «нажал ещё раз, потому что не увидел реакции»;
    * ключ ручного запуска с секундной гранулярностью — защита от двойного
      клика, когда первая проверка ещё не увидела нового статуса.

    Расписаний это НЕ касается: повторяющаяся рассылка обязана запускаться снова,
    и планировщик зовёт ``launch`` напрямую.
    """
    if campaign.status in {CampaignStatus.QUEUED.value, CampaignStatus.RUNNING.value}:
        logger.info('Campaign %s is already running, ignoring repeated launch', campaign.id)
        return None

    moment = at or datetime.now(UTC)
    return launch(campaign, run_key=manual_run_key(str(campaign.id), moment), planned_for=moment)
