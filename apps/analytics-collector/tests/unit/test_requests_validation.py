"""Валидаторы входных DTO (``models/requests.py``).

Проверяется не «pydantic умеет валидировать», а конкретные решения: какие URL
считаются допустимыми, почему запрещена вложенность в ``properties``, какое
расхождение часов клиента терпимо и что считается неправдоподобной
длительностью просмотра.
"""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from practix_analytics_collector.models.enums import ElementType, VideoQuality
from practix_analytics_collector.models.requests import (
    MAX_PROPERTIES_KEYS,
    MAX_PROPERTY_VALUE_LENGTH,
    ClickEventIn,
    VideoCompletedIn,
    VideoProgressIn,
)


def click(**overrides) -> dict:
    data = {'session_id': 'session-1', 'element_type': ElementType.FILM}
    data.update(overrides)
    return data


class TestUrlValidation:
    @pytest.mark.parametrize('url', ['http://example.com/films', 'https://example.com/'])
    def test_http_urls_are_accepted(self, url):
        assert ClickEventIn(**click(page_url=url)).page_url == url

    @pytest.mark.parametrize(
        'url',
        [
            'javascript:alert(1)',
            'data:text/html;base64,PHNjcmlwdD4=',
            'file:///etc/passwd',
            '//example.com',
        ],
    )
    def test_other_schemes_are_rejected(self, url):
        """Значение попадает в хранилище аналитики и рано или поздно будет
        отрендерено в чьём-нибудь дашборде — санитизировать надо на входе."""
        with pytest.raises(ValidationError):
            ClickEventIn(**click(page_url=url))


class TestProperties:
    def test_flat_scalar_dict_is_accepted(self):
        properties = {'ab': 'test', 'count': 3, 'ratio': 0.5, 'flag': True}
        assert ClickEventIn(**click(properties=properties)).properties == properties

    def test_nesting_is_rejected(self):
        """Произвольная вложенность позволила бы прислать глубоко рекурсивную
        структуру и раздуть сообщение в Kafka."""
        with pytest.raises(ValidationError):
            ClickEventIn(**click(properties={'nested': {'a': 1}}))

    def test_too_many_keys_are_rejected(self):
        properties = {f'key{i}': i for i in range(MAX_PROPERTIES_KEYS + 1)}
        with pytest.raises(ValidationError):
            ClickEventIn(**click(properties=properties))

    def test_oversized_value_is_rejected(self):
        with pytest.raises(ValidationError):
            ClickEventIn(**click(properties={'k': 'x' * (MAX_PROPERTY_VALUE_LENGTH + 1)}))


class TestClientTimestamp:
    def test_naive_datetime_is_treated_as_utc(self):
        value = datetime.now(UTC).replace(tzinfo=None)
        assert ClickEventIn(**click(event_timestamp=value)).event_timestamp.tzinfo is not None

    def test_far_future_is_rejected(self):
        """Часы клиента бывают сбиты, а бывают подделаны намеренно: значение из
        будущего испортило бы временные ряды в аналитике."""
        with pytest.raises(ValidationError):
            ClickEventIn(**click(event_timestamp=datetime.now(UTC) + timedelta(days=2)))

    def test_far_past_is_rejected(self):
        with pytest.raises(ValidationError):
            ClickEventIn(**click(event_timestamp=datetime.now(UTC) - timedelta(days=30)))

    def test_small_skew_is_tolerated(self):
        """Допуск существует намеренно: у части устройств часы уходят на
        минуты, и отбрасывать их события было бы дороже, чем принять."""
        value = datetime.now(UTC) - timedelta(minutes=30)
        assert ClickEventIn(**click(event_timestamp=value)).event_timestamp is not None


class TestServerControlledFieldsAreRejected:
    @pytest.mark.parametrize('field', ['user_id', 'is_authenticated', 'received_at', 'ip'])
    def test_client_cannot_set_server_fields(self, field):
        """Полей нет в модели вовсе, а ``extra='forbid'`` превращает попытку их
        передать в 422 — то есть подделка невозможна, а не «игнорируется»."""
        with pytest.raises(ValidationError):
            ClickEventIn(**click(**{field: 'anything'}))


class TestVideoProgress:
    def test_position_within_duration_is_accepted(self):
        event = VideoProgressIn(
            session_id='s',
            film_id='11111111-1111-1111-1111-111111111111',
            playback_position_ms=30_000,
            duration_ms=120_000,
            quality=VideoQuality.Q_1080P,
        )
        assert event.completion_rate == 0.25

    def test_position_beyond_duration_is_rejected(self):
        with pytest.raises(ValidationError):
            VideoProgressIn(
                session_id='s',
                film_id='11111111-1111-1111-1111-111111111111',
                playback_position_ms=200_000,
                duration_ms=120_000,
            )

    def test_small_overshoot_is_tolerated(self):
        """5 % допуска — на погрешность плеера и округление на клиенте."""
        event = VideoProgressIn(
            session_id='s',
            film_id='11111111-1111-1111-1111-111111111111',
            playback_position_ms=123_000,
            duration_ms=120_000,
        )
        assert event.completion_rate == 1.0


class TestVideoCompleted:
    def test_rewatching_is_allowed(self):
        """Перемотки назад дают watched_ms больше длительности — это норма,
        а не ошибка. Отсекается только кратно неправдоподобное значение."""
        event = VideoCompletedIn(
            session_id='s',
            film_id='11111111-1111-1111-1111-111111111111',
            duration_ms=100_000,
            watched_ms=150_000,
        )
        assert event.completion_rate == 1.0

    def test_implausible_watched_time_is_rejected(self):
        with pytest.raises(ValidationError):
            VideoCompletedIn(
                session_id='s',
                film_id='11111111-1111-1111-1111-111111111111',
                duration_ms=100_000,
                watched_ms=500_000,
            )

    def test_validation_does_not_depend_on_field_order(self):
        """Регрессия: проверка была написана через field_validator с
        ``info.data``, который видит только объявленные ВЫШЕ поля. Передача
        полей в обратном порядке (а это ровно то, что делает JSON-запрос с
        другой раскладкой ключей) не должна её отключать."""
        with pytest.raises(ValidationError):
            VideoCompletedIn.model_validate(
                {
                    'watched_ms': 500_000,
                    'duration_ms': 100_000,
                    'film_id': '11111111-1111-1111-1111-111111111111',
                    'session_id': 's',
                }
            )
