"""Сквозной сценарий: менеджер нажал «Отправить сейчас» — письмо пришло.

Ровно тот путь, который описывает теория: админ-панель создаёт событие → API
кладёт его в очередь → воркер отправляет уведомление. Здесь он проверяется
целиком и на настоящей инфраструктуре: панель, брокер, планировщик, веер,
воркер и почтовый сервер — разные процессы в разных контейнерах.
"""

from conftest import Mailpit, make_subscriber, wait_until

from practix_notifications.campaigns.models import DeliveryTask, ScheduledRun
from practix_notifications.enums import CampaignStatus, RunStatus, TaskStatus
from practix_notifications.services.launch import launch_now
from practix_notifications.subscribers.models import Subscriber


def test_letter_reaches_the_recipient(campaign, subscriber):
    run = launch_now(campaign)
    assert run is not None

    messages = Mailpit.wait_for(count=1)
    letter = Mailpit.message(messages[0]['ID'])

    assert letter['To'][0]['Address'] == subscriber.email
    # Имя приехало из Auth: в локальной витрине его нет и колонки под него нет.
    assert 'Привет, Иван' in letter['Subject']
    # Переменная кампании подставилась в тело — значит шаблонизатор отработал на
    # объединённом контексте рассылки и получателя.
    assert 'Дюна' in letter['HTML']
    # Текстовая часть обязательна: без неё письмо у половины клиентов выглядит
    # как вложение.
    assert letter['Text'].strip()


def test_delivery_is_recorded_in_the_journal(campaign, subscriber):
    launch_now(campaign)
    Mailpit.wait_for(count=1)

    task = wait_until(
        lambda: DeliveryTask.objects.filter(status=TaskStatus.SENT.value).first(),
        message='Задача доставки не перешла в «отправлено»',
    )
    # Адрес на задаче проставил формирующий воркер, получив его от Auth: веер
    # его больше не пишет, а журнал обязан показывать, куда ушло письмо.
    assert task.address == subscriber.email
    assert task.sent_at is not None
    assert task.delivery_attempts.count() == 1
    # Идентификатор запроса сшивает журнал с логами панели и воркера.
    assert task.delivery_attempts.first().result == 'sent'


def test_campaign_counters_and_statuses_converge(campaign):
    make_subscriber('petr', 'petr@example.com', first_name='Пётр', last_name='Сидоров')
    make_subscriber('anna', 'anna@example.com', first_name='Анна', last_name='Кузнецова')

    launch_now(campaign)
    Mailpit.wait_for(count=2)

    wait_until(
        lambda: campaign.__class__.objects.get(pk=campaign.pk).status == CampaignStatus.DONE.value,
        message='Рассылка не перешла в «завершена»',
    )
    refreshed = campaign.__class__.objects.get(pk=campaign.pk)
    assert refreshed.recipients_total == 2
    assert refreshed.sent_count == 2
    assert refreshed.failed_count == 0
    assert refreshed.last_notification_sent_at is not None

    run = ScheduledRun.objects.get(campaign=campaign)
    assert run.status == RunStatus.PUBLISHED.value
    assert run.tasks_created == 2


def test_letter_is_built_from_the_run_snapshot(campaign, subscriber):
    """Правка шаблона после запуска не переписывает уже запущенную рассылку.

    Иначе повторяющаяся рассылка расщеплялась бы на «до» и «после»: часть
    получателей со старым текстом, часть с новым.
    """
    launch_now(campaign)
    campaign.template.subject_template = 'СОВЕРШЕННО ДРУГАЯ ТЕМА'
    campaign.template.save(update_fields=['subject_template'])

    messages = Mailpit.wait_for(count=1)
    assert 'ДРУГАЯ' not in Mailpit.message(messages[0]['ID'])['Subject']


def test_double_click_does_not_double_send(campaign, subscriber):
    """Два нажатия кнопки в пределах секунды — одна рассылка."""
    first = launch_now(campaign)
    second = launch_now(campaign)

    assert first is not None
    assert second is None, 'Ключ ручного запуска не свернул повторный клик'
    Mailpit.wait_for(count=1)
    assert ScheduledRun.objects.filter(campaign=campaign).count() == 1


def test_inactive_subscriber_is_skipped(campaign, subscriber):
    Subscriber.objects.filter(pk=subscriber.pk).update(is_active=False)
    launch_now(campaign)

    wait_until(
        lambda: campaign.__class__.objects.get(pk=campaign.pk).status == CampaignStatus.DONE.value,
        message='Рассылка не завершилась',
    )
    # Аудитория пуста — веер отфильтровал неактивного ещё до очереди.
    assert DeliveryTask.objects.count() == 0
    assert not Mailpit.messages()
