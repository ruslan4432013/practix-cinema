"""Имя в письме приезжает из Auth, а не из локальной витрины.

Это и есть проверяемое требование задания: воркер получает из очереди только
идентификатор получателя и сам ходит в сервис авторизации за данными для
персонифицированного сообщения. Всё здесь — на настоящих контейнерах: Auth,
брокер, формирующий воркер, отправляющий воркер, почтовый сервер.
"""

import uuid
from datetime import UTC, datetime, timedelta

from conftest import Mailpit, auth_login, make_subscriber, set_user_names, wait_until

from practix_notifications.campaigns.models import DeliveryTask
from practix_notifications.enums import CampaignStatus, SkipReason
from practix_notifications.services.launch import launch_now
from practix_notifications.subscribers.models import Subscriber


def test_name_comes_from_auth_and_not_from_the_local_view(campaign, subscriber):
    """Витрина знает логин и адрес, имя знает только Auth."""
    # В локальной строке подписчика имени нет физически — под него нет колонки.
    assert not hasattr(subscriber, 'first_name')

    launch_now(campaign)
    messages = Mailpit.wait_for(count=1)
    letter = Mailpit.message(messages[0]['ID'])

    assert 'Привет, Иван' in letter['Subject']
    assert 'Иван Петров' in letter['HTML']


def test_name_change_in_auth_reaches_the_next_letter(campaign, subscriber):
    """Данные берутся в момент сборки, а не замораживаются в очереди.

    Кеш резолва на стенде выключен (NOTIFY_DIRECTORY_CACHE_TTL=0) — именно
    затем, чтобы это свойство было видно, а не спрятано за пятиминутным TTL.
    """
    launch_now(campaign)
    Mailpit.wait_for(count=1)

    set_user_names(auth_login('ivan'), first_name='Иоганн', last_name='Себастьянов')
    Mailpit.clear()
    # Рассылка завершена первым запуском; сбрасываем статус, чтобы запустить её
    # второй раз.
    campaign.status = CampaignStatus.DRAFT.value
    campaign.save(update_fields=['status'])

    # Момент второго запуска задаётся явно. Ключ ручного запуска гранулярен до
    # секунды (защита от двойного клика), и на быстром раннере вся первая
    # доставка успевает уложиться в ту же секунду — тогда второй launch_now
    # получил бы тот же ключ и был бы отброшен как дубль.
    launch_now(campaign, at=datetime.now(UTC) + timedelta(seconds=1))
    messages = Mailpit.wait_for(count=1)

    assert 'Привет, Иоганн' in Mailpit.message(messages[0]['ID'])['Subject']


def test_user_without_a_name_is_greeted_by_login(campaign):
    """«Здравствуйте, !» хуже, чем обращение по нику."""
    make_subscriber('nameless', 'nameless@example.com', first_name='', last_name='')

    launch_now(campaign)
    messages = Mailpit.wait_for(count=1)

    assert 'Привет, nameless' in Mailpit.message(messages[0]['ID'])['Subject']


def test_subscriber_unknown_to_auth_gets_no_letter(campaign):
    """Подписчик, существующий только в витрине, — не повод отправлять письмо.

    Отдельная причина пропуска: менеджер должен видеть «нет в Auth», а не
    гадать между «отписался» и «нет адреса». И, что важнее, прогон обязан
    закрыться — иначе рассылка навсегда осталась бы «идущей».
    """
    Subscriber.objects.create(id=uuid.uuid4(), login='ghost', email='ghost@example.com')

    launch_now(campaign)

    task = wait_until(
        lambda: DeliveryTask.objects.filter(skip_reason=SkipReason.UNKNOWN_USER.value).first(),
        message='задача не отмечена как unknown_user',
    )
    assert task.address == ''
    wait_until(
        lambda: campaign.__class__.objects.get(pk=campaign.pk).status == CampaignStatus.DONE.value,
        message='рассылка не закрылась после отсева всех получателей',
    )
    Mailpit.assert_stable(count=0, seconds=3)


def test_address_is_filled_by_the_builder_not_by_the_fan_out(campaign, subscriber):
    """Адрес на задаче появляется после резолва в Auth, а не при создании задачи.

    Веер его больше не пишет: локальная копия годится, чтобы решить, кого
    включать в аудиторию, но не чтобы отправить по ней письмо.
    """
    launch_now(campaign)
    Mailpit.wait_for(count=1)

    task = wait_until(lambda: DeliveryTask.objects.first(), message='задача доставки не создана')
    assert task.address == subscriber.email
