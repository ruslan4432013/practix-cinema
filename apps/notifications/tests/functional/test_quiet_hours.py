"""Тихие часы на живом стенде: письмо откладывается, а не теряется.

До этого набора тихие часы были покрыты только юнитом — то есть проверялась
арифметика окна, но не то, что происходит с письмом на самом деле. А происходит
там довольно много: получатель уезжает в парковочную очередь по ветке ``ACK``,
возвращается через ``x-message-ttl`` и заново проходит проверку по АКТУАЛЬНОЙ
строке подписчика.

Именно тут жила вторая половина правки бюджета повторов. Отсрочка публиковалась
с увеличенным номером попытки, а восьмичасовое ночное окно при тридцатисекундном
TTL парковки — это около тысячи отскоков: пачка доживала до утра с ``x-attempt``
под тысячу и уходила в dead-letters от первой же настоящей ошибки. Здесь ночь
сжата до нескольких секунд, но механизм тот же самый.

Детерминизм обеспечивается двумя вещами: окно на стенде накрывает сутки целиком
(``docker-compose.test.yml``), а общая фикстура ``campaign`` от тихих часов
отписана. Поэтому «ночь» здесь не зависит от того, в котором часу запущен набор.
"""

from conftest import Mailpit, wait_until

from practix_notifications.campaigns.models import Campaign, DeliveryTask
from practix_notifications.content.models import MessageTemplate
from practix_notifications.enums import Channel, TaskStatus
from practix_notifications.services.launch import launch_now


def _nightly_campaign(template: MessageTemplate) -> Campaign:
    """Рассылка, которая тихие часы уважает, — в отличие от общей фикстуры."""
    return Campaign.objects.create(
        name='Ночная рассылка',
        channel=Channel.EMAIL.value,
        template=template,
        context={'film_title': 'Дюна'},
        respect_quiet_hours=True,
    )


def test_letter_inside_the_window_is_deferred_not_dropped(template, subscriber):
    """Отсрочка — не отказ: задача возвращается в ожидание, письма пока нет."""
    launch_now(_nightly_campaign(template))

    task = wait_until(
        lambda: DeliveryTask.objects.filter(status=TaskStatus.PENDING.value).first(),
        message='Задача не вернулась в ожидание по тихим часам',
    )
    assert task.skip_reason == '', 'тихие часы — это отсрочка, а не причина пропуска'
    assert task.sent_at is None
    # Письма нет и не появится, пока окно не кончится.
    Mailpit.assert_stable(count=0, seconds=4)


def test_deferral_does_not_burn_the_retry_budget(template, subscriber):
    """Главное свойство: отскоки не приближают пачку к dead-letters.

    ``NOTIFY_MAX_ATTEMPTS`` на стенде равен двум, а TTL парковки — двум секундам.
    Раньше двух отскоков хватало, чтобы исчерпать бюджет; за восемь секунд их
    здесь набирается заметно больше — и задача обязана всё ещё быть живой, а не
    лежать в dead-letters.
    """
    launch_now(_nightly_campaign(template))
    task = wait_until(
        lambda: DeliveryTask.objects.filter(status=TaskStatus.PENDING.value).first(),
        message='Задача не вернулась в ожидание по тихим часам',
    )

    Mailpit.assert_stable(count=0, seconds=8)

    task.refresh_from_db()
    assert task.status == TaskStatus.PENDING.value, 'отложенная задача не должна становиться терминальной'
    assert task.attempts == 0, 'отсрочка — не попытка отправки'


def test_morning_releases_the_letter(template, subscriber):
    """Наступило утро — письмо ушло само, без повторного запуска рассылки.

    «Утро» здесь наступает у КОНКРЕТНОГО получателя: окно считается в его
    таймзоне, а воркер перечитывает её из свежей строки подписчика на каждом
    отскоке — именно чтобы такая правка сработала без перезапуска чего-либо.
    """
    campaign = _nightly_campaign(template)
    launch_now(campaign)
    wait_until(
        lambda: DeliveryTask.objects.filter(status=TaskStatus.PENDING.value).first(),
        message='Задача не вернулась в ожидание по тихим часам',
    )

    # Рассвет: рассылку отписываем от тихих часов, и следующий отскок это увидит.
    Campaign.objects.filter(pk=campaign.pk).update(respect_quiet_hours=False)

    messages = Mailpit.wait_for(count=1)
    assert messages, 'после окончания тихих часов письмо обязано уйти'
    wait_until(
        lambda: DeliveryTask.objects.filter(status=TaskStatus.SENT.value).exists(),
        message='Задача не перешла в «отправлено» после окончания тихих часов',
    )
