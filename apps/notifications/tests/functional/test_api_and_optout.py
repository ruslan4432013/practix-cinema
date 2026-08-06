"""Приём событий извне и настройки уведомлений пользователя.

Первое — сценарий теории «любая часть сайта просит отправить уведомление»:
источником рассылки может быть не только кнопка в админке.
Второе — требование «должна быть возможность настройки уведомлений
пользователем, в том числе отключение».
"""

import requests
from conftest import NOTIFICATIONS_URL, Mailpit, wait_until

from practix_notifications.campaigns.models import DeliveryTask, ScheduledRun
from practix_notifications.core.config import settings
from practix_notifications.enums import Channel, SkipReason, TaskStatus
from practix_notifications.services.links import unsubscribe_url
from practix_notifications.subscribers.models import ChannelOptout


def _post_event(payload: dict, token: str | None = None) -> requests.Response:
    headers = {'Content-Type': 'application/json'}
    if token is not None:
        headers['X-Internal-Token'] = token
    return requests.post(f'{NOTIFICATIONS_URL}/api/v1/notifications/events', json=payload, headers=headers, timeout=10)


class TestHealth:
    def test_liveness_does_not_touch_dependencies(self):
        response = requests.get(f'{NOTIFICATIONS_URL}/health/live', timeout=5)
        assert response.status_code == 200
        assert response.json()['status'] == 'ok'

    def test_readiness_reports_the_database(self):
        response = requests.get(f'{NOTIFICATIONS_URL}/health/ready', timeout=5)
        assert response.status_code == 200
        assert response.json()['checks']['database'] == 'ok'

    def test_request_id_is_generated_when_missing(self):
        """Панель живёт без Nginx, и браузер заголовок не шлёт.

        В режиме reject_400 (как у контентной админки) админка отвечала бы 400
        на каждой странице, включая форму логина.
        """
        response = requests.get(f'{NOTIFICATIONS_URL}/health/live', timeout=5)
        assert response.status_code == 200
        assert response.headers.get('X-Request-Id')


class TestIntakeApi:
    def test_event_launches_the_campaign(self, campaign, subscriber):
        response = _post_event({'campaign_id': str(campaign.id)}, token=settings.NOTIFY_INTAKE_TOKEN)
        assert response.status_code == 202
        assert response.json()['run_id']

        Mailpit.wait_for(count=1)
        assert ScheduledRun.objects.filter(campaign=campaign).count() == 1

    def test_wrong_token_is_rejected(self, campaign):
        # Токен латиницей: значения HTTP-заголовков кодируются latin-1, и
        # кириллица упала бы ещё на стороне клиента, не дойдя до проверки.
        assert _post_event({'campaign_id': str(campaign.id)}, token='definitely-wrong').status_code == 401

    def test_missing_token_is_rejected(self, campaign):
        assert _post_event({'campaign_id': str(campaign.id)}).status_code == 401

    def test_unknown_campaign_is_404(self):
        response = _post_event(
            {'campaign_id': '00000000-0000-0000-0000-000000000000'}, token=settings.NOTIFY_INTAKE_TOKEN
        )
        assert response.status_code == 404

    def test_missing_campaign_id_is_400(self):
        assert _post_event({}, token=settings.NOTIFY_INTAKE_TOKEN).status_code == 400

    def test_repeat_within_a_second_is_reported_as_duplicate(self, campaign, subscriber):
        first = _post_event({'campaign_id': str(campaign.id)}, token=settings.NOTIFY_INTAKE_TOKEN)
        second = _post_event({'campaign_id': str(campaign.id)}, token=settings.NOTIFY_INTAKE_TOKEN)
        assert first.status_code == 202
        assert second.status_code == 200
        assert second.json()['status'] == 'duplicate'


class TestOptout:
    def test_opted_out_subscriber_gets_no_letter(self, campaign, subscriber):
        ChannelOptout.objects.create(subscriber=subscriber, channel=Channel.EMAIL.value, category='')

        from practix_notifications.services.launch import launch_now

        launch_now(campaign)

        task = wait_until(
            lambda: DeliveryTask.objects.filter(status=TaskStatus.SKIPPED.value).first(),
            message='Задача не была пропущена по отписке',
        )
        assert task.skip_reason == SkipReason.OPTED_OUT.value
        Mailpit.assert_stable(count=0, seconds=3)

    @staticmethod
    def _unsubscribe_endpoint(subscriber) -> str:
        url = unsubscribe_url(str(subscriber.id), Channel.EMAIL.value, '')
        # Ссылка ведёт на публичный адрес; в тестовой сети ходим по внутреннему.
        path = url.split('/api/v1/notifications/', 1)[1]
        return f'{NOTIFICATIONS_URL}/api/v1/notifications/{path}'

    def test_get_only_asks_and_changes_nothing(self, subscriber):
        """По ссылке из письма первым ходит не человек.

        Outlook Safe Links, антивирусные шлюзы и префетчеры почтовых клиентов
        открывают всё, что нашли в теле. Отписка по GET означала бы, что часть
        получателей отписывается, никогда письма не открыв, а менеджер видит
        необъяснимый обвал аудитории.
        """
        response = requests.get(self._unsubscribe_endpoint(subscriber), timeout=5)

        assert response.status_code == 200
        assert '<form' in response.text and 'method="post"' in response.text
        assert not ChannelOptout.objects.filter(subscriber=subscriber).exists()

    def test_post_performs_the_unsubscribe(self, subscriber):
        response = requests.post(self._unsubscribe_endpoint(subscriber), timeout=5)

        assert response.status_code == 200
        assert ChannelOptout.objects.filter(subscriber=subscriber, channel=Channel.EMAIL.value).exists()

    def test_unsubscribing_twice_is_not_an_error(self, subscriber):
        """Кнопку нажимают дважды, и второй раз это не должно быть 500."""
        endpoint = self._unsubscribe_endpoint(subscriber)
        assert requests.post(endpoint, timeout=5).status_code == 200
        assert requests.post(endpoint, timeout=5).status_code == 200
        assert ChannelOptout.objects.filter(subscriber=subscriber).count() == 1

    def test_forged_unsubscribe_token_is_rejected(self):
        base = f'{NOTIFICATIONS_URL}/api/v1/notifications/unsubscribe/подделка'
        assert requests.get(base, timeout=5).status_code == 404
        assert requests.post(base, timeout=5).status_code == 404
