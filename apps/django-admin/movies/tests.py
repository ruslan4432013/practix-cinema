"""Продюсер отчётного события «в кинотеатре появился новый фильм».

Второй из двух продюсеров доменных событий в репозитории (первый — Auth,
`apps/auth/tests/unit/test_notifications_client.py`), и до сих пор единственный
непокрытый. Проверять здесь есть что: у обработчика три условия отсечения,
отложенный на коммит запуск потока и требование НИКОГДА не ронять сохранение
фильма, что бы ни ответили нотификации.

## Почему поток приходится подменять

`transaction.on_commit` в `TestCase` не срабатывает вовсе: тест идёт внутри
транзакции, которая откатывается, — коммита не наступает. Поэтому
`captureOnCommitCallbacks(execute=True)` выполняет отложенные колбэки явно, а
`threading.Thread` подменяется на синхронный запуск: настоящий поток сделал бы
набор гоночным (проверка успевала бы раньше запроса) и оставил бы демона жить
после теста.
"""

import uuid
from datetime import date
from unittest import mock

import requests
from django.db.models.signals import post_save
from django.test import TestCase, override_settings

from movies.models import FilmWork
from movies.signals import EVENT_FILM_PUBLISHED, _post_event

ENABLED = {
    'NOTIFY_EVENTS_ENABLED': True,
    'NOTIFY_API_URL': 'http://notifications-admin:8000',
    'NOTIFY_INTAKE_TOKEN': 'test-internal-token',
    'NOTIFY_EVENT_TIMEOUT': 1.0,
    'NOTIFY_EVENT_MAX_ATTEMPTS': 2,
}


def _response(status_code: int, text: str = ''):
    response = mock.Mock()
    response.status_code = status_code
    response.text = text
    return response


class _SyncThread:
    """Замена `threading.Thread`, выполняющая цель немедленно при `start()`."""

    def __init__(self, target=None, args=(), **kwargs):
        self._target = target
        self._args = args

    def start(self) -> None:
        self._target(*self._args)


class FilmPublishedSignalTests(TestCase):
    """Три условия отсечения — каждое закрывает свой ложный вызов."""

    def setUp(self):
        patcher = mock.patch('movies.signals.threading.Thread', _SyncThread)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _save_film(self, **overrides) -> FilmWork:
        # `rating` и `creation_date` объявлены `blank=True`, но НЕ `null=True` —
        # и миграция это подтверждает, поэтому значения обязательны.
        payload = {'title': 'Дюна', 'type': 'movie', 'creation_date': date(2021, 9, 15), 'rating': 8.0}
        payload.update(overrides)
        with self.captureOnCommitCallbacks(execute=True):
            return FilmWork.objects.create(**payload)

    @override_settings(**ENABLED)
    @mock.patch('movies.signals.request_with_retry')
    def test_new_film_emits_the_event(self, request):
        request.return_value = _response(202)

        film = self._save_film()

        assert request.call_count == 1
        method, url = request.call_args.args
        assert method == 'POST'
        assert url == 'http://notifications-admin:8000/api/v1/notifications/events'

        body = request.call_args.kwargs['json']
        assert body['type'] == EVENT_FILM_PUBLISHED
        assert body['data'] == {'film_id': str(film.id), 'title': 'Дюна', 'year': 2021}
        assert body['event_id'] and body['occurred_at']
        # Секрет тот же и с тем же именем, что у Auth и у самих нотификаций.
        assert request.call_args.kwargs['headers']['X-Internal-Token'] == 'test-internal-token'

    @override_settings(**ENABLED)
    @mock.patch('movies.signals.request_with_retry')
    def test_editing_a_film_emits_nothing(self, request):
        """Правка описания или рейтинга не делает фильм новым."""
        request.return_value = _response(202)
        film = self._save_film()
        request.reset_mock()

        film.description = 'Правка'
        with self.captureOnCommitCallbacks(execute=True):
            film.save()

        assert request.call_count == 0

    @override_settings(**ENABLED)
    @mock.patch('movies.signals.request_with_retry')
    def test_loaddata_emits_nothing(self, request):
        """`raw=True` — это загрузка фикстур. Фикстура не событие."""
        post_save.send(sender=FilmWork, instance=FilmWork(title='Из фикстуры'), created=True, raw=True)

        assert request.call_count == 0

    @mock.patch('movies.signals.request_with_retry')
    def test_disabled_flag_emits_nothing(self, request):
        """Стенд без профиля `notifications`: такого хоста нет в сети.

        Выключено по умолчанию — `override_settings` здесь намеренно нет.
        """
        self._save_film()

        assert request.call_count == 0

    @override_settings(**ENABLED)
    @mock.patch('movies.signals.request_with_retry')
    def test_film_without_a_creation_date_reports_a_null_year(self, request):
        """Ветка `... if instance.creation_date else None` — не мёртвая страховка.

        Через `objects.create` сюда не попасть: в базе колонка NOT NULL. Но
        `post_save` шлют и мимо ORM — из `loaddata`, из чужого кода, из
        миграции данных, — и обработчик обязан пережить экземпляр без даты, а не
        уронить `AttributeError` в фоновом потоке, где его никто не поймает.
        """
        request.return_value = _response(202)

        with self.captureOnCommitCallbacks(execute=True):
            post_save.send(
                sender=FilmWork,
                instance=FilmWork(title='Без даты', type='movie', creation_date=None),
                created=True,
            )

        assert request.call_args.kwargs['json']['data']['year'] is None

    @override_settings(**ENABLED)
    @mock.patch('movies.signals.request_with_retry')
    def test_each_film_gets_its_own_event_id(self, request):
        request.return_value = _response(202)

        self._save_film(title='Первый')
        self._save_film(title='Второй')

        ids = {call.kwargs['json']['event_id'] for call in request.call_args_list}
        assert len(ids) == 2


class FilmPublishedFailureTests(TestCase):
    """Каталог обязан работать без сервиса нотификаций.

    Исключение, вышедшее из фонового потока, никем не будет поймано и ничего не
    чинит, поэтому `_post_event` не бросает ничего и никогда. Проверяется он
    напрямую: в бою его вызывает поток, а здесь важна именно его собственная
    устойчивость.
    """

    PAYLOAD = {'type': EVENT_FILM_PUBLISHED, 'data': {'film_id': str(uuid.uuid4())}}

    @override_settings(**ENABLED)
    @mock.patch('movies.signals.request_with_retry')
    def test_network_failure_is_swallowed(self, request):
        request.side_effect = requests.ConnectionError('нотификации недоступны')

        _post_event(self.PAYLOAD)  # не должно бросить

    @override_settings(**ENABLED)
    @mock.patch('movies.signals.request_with_retry')
    def test_unexpected_error_is_swallowed_too(self, request):
        """`except Exception` здесь осознанно широкий: это фоновый поток."""
        request.side_effect = TypeError('внезапная ошибка программиста')

        _post_event(self.PAYLOAD)

    @override_settings(**ENABLED)
    @mock.patch('movies.signals.request_with_retry')
    def test_a_nonsense_response_is_swallowed_too(self, request):
        """Разбор ответа тоже под `try`, и это не придирка.

        Проверка кода ответа стояла СНАРУЖИ, поэтому объект, у которого
        `status_code` не число, вылетал исключением прямо в поток — где его
        некому поймать, а обещание «не бросает» переставало быть верным.
        """
        request.return_value = mock.Mock()  # status_code — тоже Mock, не число

        _post_event(self.PAYLOAD)

    @override_settings(**ENABLED)
    @mock.patch('movies.signals.request_with_retry')
    def test_rejection_is_logged_and_not_retried(self, request):
        """4xx повтором не лечится, а 5xx уже переспрошен внутри `request_with_retry`.

        Дедупликация на приёме идёт по `film_id`, поэтому анонс можно запустить
        руками из панели рассылок, не рискуя вторым письмом.
        """
        request.return_value = _response(400, 'binding disabled')

        with self.assertLogs('movies.signals', level='ERROR') as logs:
            _post_event(self.PAYLOAD)

        assert request.call_count == 1
        assert 'binding disabled' in logs.output[0]

    @override_settings(**{**ENABLED, 'NOTIFY_API_URL': 'http://notifications-admin:8000/'})
    @mock.patch('movies.signals.request_with_retry')
    def test_trailing_slash_in_the_base_url_does_not_double_up(self, request):
        request.return_value = _response(202)

        _post_event(self.PAYLOAD)

        assert request.call_args.args[1] == 'http://notifications-admin:8000/api/v1/notifications/events'
