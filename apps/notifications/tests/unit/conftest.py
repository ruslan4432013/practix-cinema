"""Общие фикстуры юнит-набора.

Минимальные объекты, без которых не построить ни задачу доставки, ни прогон:
шаблон, рассылка и подписчик. Держать их здесь, а не по копии в каждом файле, —
не только вопрос вкуса: одинаковые фикстуры в трёх файлах ловятся порогом
дублирования в CI как настоящий клон.
"""

import uuid

import pytest

from practix_notifications.campaigns.models import Campaign
from practix_notifications.content.models import MessageTemplate
from practix_notifications.core.config import RATE_SCOPE_PROCESS, settings
from practix_notifications.subscribers.models import Subscriber


@pytest.fixture(autouse=True)
def _process_rate_scope(monkeypatch) -> None:
    """Юнит-набор обязан проходить на голом раннере, где Redis нет.

    По умолчанию темп отправки общий на сервис и живёт в Redis (см.
    ``channels/pacing.py``); здесь он принудительно попроцессный, иначе каждое
    письмо в тестах отправителя начиналось бы с похода в несуществующее
    хранилище. Сам общий режим проверяет ``test_pacing.py`` на заглушке клиента и
    функциональный набор — на настоящем Redis.
    """
    monkeypatch.setattr(settings, 'NOTIFY_SMTP_RATE_SCOPE', RATE_SCOPE_PROCESS)


@pytest.fixture
def template() -> MessageTemplate:
    return MessageTemplate.objects.create(code='t', name='Шаблон', subject_template='Тема', body_template='<p>Тело</p>')


@pytest.fixture
def campaign(template) -> Campaign:
    return Campaign.objects.create(name='Рассылка', template=template)


@pytest.fixture
def subscriber() -> Subscriber:
    return Subscriber.objects.create(id=uuid.uuid4(), login='ivan', email='ivan@example.com', timezone='Europe/Moscow')
