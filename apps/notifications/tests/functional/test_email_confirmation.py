"""Сквозной путь темы «Короткие ссылки»: регистрация → письмо → подтверждение.

Тест, который и доказывает задание целиком, потому что в нём участвуют ВСЕ
звенья и ни одно не подменено: Auth выдаёт пользователя, интейк принимает
событие, планировщик сливает outbox, веер публикует идентификаторы, формирующий
воркер выпускает КОРОТКУЮ ссылку в отдельном сервисе и вставляет её в письмо,
Mailpit его принимает, а переход по ссылке возвращается в Auth и ставит там флаг.

Живёт в наборе нотификаций, а не шортенера, намеренно: стенд с Auth, брокером и
почтой стоит здесь. Второй такой стенд ради одного файла означал бы вторую копию
четырёхсотстрочного conftest — крупнейшее дублирование в репозитории.
"""

import os
import re
import uuid

import pytest
import requests
from conftest import (
    AUTH_URL,
    Mailpit,
    auth_login,
    post_event,
    register_user,
    wait_until,
)

from practix_notifications.content.models import MessageTemplate
from practix_notifications.enums import Channel, DomainEvent

SHORTENER_URL = os.getenv('SHORTENER_URL', 'http://link-shortener:8000').rstrip('/')

#: Адрес, на который сервис ссылок уводит после подтверждения. Совпадает с
#: `SHORTENER_ALLOWED_REDIRECT_HOSTS`: белый список — не формальность, чужой хост
#: сервис бы просто не принял.
REDIRECT_URL = 'http://localhost/'

#: Ссылка вида http://localhost/s/AbC1234 внутри HTML письма.
SHORT_LINK_RE = re.compile(r'https?://[^/"\s]+/s/([0-9A-Za-z]+)')


@pytest.fixture
def confirm_template() -> MessageTemplate:
    """Шаблон, который ПРОСИТ `confirm_url` — именно это включает поход в шортенер."""
    return MessageTemplate.objects.create(
        code='functional-confirm',
        name='Подтверждение адреса',
        channel=Channel.EMAIL.value,
        subject_template='Подтвердите адрес, {{ display_name }}',
        body_template='<p>Привет, {{ full_name }}!</p><p><a href="{{ confirm_url }}">Подтвердить</a></p>',
        allowed_variables=['login', 'email', 'first_name', 'last_name', 'full_name', 'display_name', 'confirm_url'],
        sample_context={
            'login': 'ivan',
            'display_name': 'Иван',
            'full_name': 'Иван Петров',
            'confirm_url': 'http://x/s/a',
        },
    )


@pytest.fixture
def confirm_campaign(confirm_template, campaign):
    """Кампания приветственного письма с настроенным в панели redirectUrl."""
    campaign.template = confirm_template
    campaign.confirm_redirect_url = REDIRECT_URL
    campaign.save(update_fields=['template', 'confirm_redirect_url'])
    return campaign


def _registration(user: dict) -> dict:
    return {
        'type': DomainEvent.USER_REGISTERED.value,
        'event_id': str(uuid.uuid4()),
        'occurred_at': '2026-08-05T10:00:00+00:00',
        'data': {'user_id': user['id'], 'login': user['login'], 'email': user['email']},
    }


def _short_link_from_letter() -> str:
    message = Mailpit.message(Mailpit.wait_for(count=1)[0]['ID'])
    body = message.get('HTML') or message.get('Text') or ''
    match = SHORT_LINK_RE.search(body)
    assert match, f'В письме нет короткой ссылки: {body[:500]}'
    return match.group(0)


def _link_info_when_visits_reach(code: str, headers: dict, *, count: int) -> dict | None:
    """Карточка ссылки, но только когда счётчик визитов дорос до ``count``."""
    response = requests.get(f'{SHORTENER_URL}/api/v1/links/{code}', headers=headers, timeout=10)
    assert response.status_code == 200
    body = response.json()
    return body if body['visit_count'] == count else None


def _me(token: str) -> dict:
    response = requests.get(
        f'{AUTH_URL}/api/v1/users/me',
        headers={'Authorization': f'Bearer {token}', 'X-Request-Id': 'functional-me'},
        timeout=10,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_welcome_letter_carries_a_short_confirmation_link(welcome_binding, confirm_campaign):
    """Ссылка в письме — КОРОТКАЯ, а не подписанный токен на две сотни символов."""
    user = register_user('confirmee', 'confirmee@example.com')
    post_event(_registration(user))

    link = _short_link_from_letter()

    assert '/s/' in link
    # Именно ради этого числа задание и заведено: ссылка должна быть удобной на
    # мобильном. Подписанный токен с UUID, меткой времени и целевым адресом
    # занял бы примерно двести символов.
    assert len(link) < 60


def test_following_the_link_confirms_the_email_and_redirects(welcome_binding, confirm_campaign):
    user = register_user('confirmee', 'confirmee@example.com')
    token = auth_login('confirmee')
    assert _me(token)['email_verified'] is False

    post_event(_registration(user))
    link = _short_link_from_letter()

    response = requests.get(link, allow_redirects=False, timeout=10)

    assert response.status_code == 302
    # Редирект случается ПОСЛЕ подтверждения, и ведёт он туда, куда настроил
    # менеджер в панели (Campaign.confirm_redirect_url).
    assert response.headers['Location'] == REDIRECT_URL
    assert _me(token)['email_verified'] is True


def test_second_click_still_redirects(welcome_binding, confirm_campaign):
    """Идемпотентность: по ссылке кликают дважды, а до человека ходит сканер почты."""
    user = register_user('confirmee', 'confirmee@example.com')
    post_event(_registration(user))
    link = _short_link_from_letter()

    first = requests.get(link, allow_redirects=False, timeout=10)
    second = requests.get(link, allow_redirects=False, timeout=10)

    assert first.status_code == 302
    assert second.status_code == 302
    assert second.headers['Location'] == REDIRECT_URL


def test_visits_are_counted(welcome_binding, confirm_campaign, shortener_headers):
    """Счёт визитов — половина задания: ссылка несёт id пользователя ради него."""
    user = register_user('confirmee', 'confirmee@example.com')
    post_event(_registration(user))
    code = _short_link_from_letter().rsplit('/', 1)[-1]

    for _ in range(3):
        requests.get(f'{SHORTENER_URL}/s/{code}', allow_redirects=False, timeout=10)

    # Счётчик инкрементится в BackgroundTasks — то есть ПОСЛЕ того, как ответ уже
    # отдан клиенту (иначе переход по ссылке ждал бы записи в базу). Значит между
    # третьим редиректом и видимой тройкой есть зазор, и его надо переждать, а не
    # предполагать, что его нет.
    body = wait_until(
        lambda: _link_info_when_visits_reach(code, shortener_headers, count=3),
        message='визиты не досчитались до трёх',
    )
    # Три составные части, которые задание требует от ссылки. Код — непрозрачный
    # ключ к ним, и вот они.
    assert body['user_id'] == user['id']
    assert body['target_url'] == REDIRECT_URL
    assert body['expires_at']


def test_invalid_code_answers_an_html_404_while_a_live_one_still_works(welcome_binding, confirm_campaign):
    """404-страница отдаётся не всем подряд.

    Само ПРОТУХАНИЕ проверяет функциональный набор шортенера
    (``test_redirect.py::test_expired_link_is_404``) — там есть доступ к его
    базе, чтобы отмотать ``expires_at``. Здесь проверяется, что живая и
    несуществующая ссылки на общем стенде ведут себя по-разному.
    """
    user = register_user('confirmee', 'confirmee@example.com')
    post_event(_registration(user))
    code = _short_link_from_letter().rsplit('/', 1)[-1]

    unknown = requests.get(f'{SHORTENER_URL}/s/zzzzzzz', allow_redirects=False, timeout=10)
    assert unknown.status_code == 404
    assert unknown.headers['content-type'].startswith('text/html')

    alive = requests.get(f'{SHORTENER_URL}/s/{code}', allow_redirects=False, timeout=10)
    assert alive.status_code == 302


def test_a_letter_without_the_variable_carries_no_link(welcome_binding, campaign):
    """Шаблон без ``confirm_url`` не должен заводить ссылку на каждого получателя.

    Массовая рассылка на сто тысяч адресов иначе оставляла бы сто тысяч строк в
    сервисе ссылок, из которых по назначению не пригодилась бы ни одна.
    """
    user = register_user('plainuser', 'plainuser@example.com')
    post_event(_registration(user))

    message = Mailpit.message(Mailpit.wait_for(count=1)[0]['ID'])
    body = message.get('HTML') or message.get('Text') or ''

    assert SHORT_LINK_RE.search(body) is None


@pytest.fixture
def shortener_headers() -> dict[str, str]:
    from practix_notifications.core.config import settings

    return {'Authorization': f'Bearer {settings.NOTIFY_SHORTENER_INTERNAL_TOKEN}'}
