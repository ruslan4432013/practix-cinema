"""Проверки инициализации сбора ошибок.

Главный охраняемый здесь регресс — не «Sentry не заводится» (это видно сразу по
пустому интерфейсу), а тихая ПОДМЕНА ГЛОБАЛЬНОГО ``TracerProvider``. Достаточно
кому-нибудь передать ``traces_sample_rate`` или добавить
``OpenTelemetryIntegration`` в список интеграций — и spans поедут в Sentry вместо
Jaeger. Ни один функциональный набор этого не поймает: сервисы продолжат
работать, а трассировка просто опустеет.

Второй регресс — очистка чувствительных полей: ``include_local_variables``
включён, и в кадрах стека едут локальные переменные вызывающего кода.
"""

import logging

import pytest
import sentry_sdk
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from practix_core import sentry
from practix_core.context import request_id_ctx

DSN = 'https://public@localhost:9/1'


@pytest.fixture(autouse=True)
def _reset():
    sentry.reset_for_testing()
    yield
    sentry.reset_for_testing()
    # Клиент, оставленный включённым, продолжает жить в глобальном скоупе и
    # утекает в соседние тесты: следующий init_sentry увидел бы чужой клиент.
    sentry_sdk.get_global_scope().set_client(None)


def _init(**overrides) -> None:
    kwargs = {
        'enabled': True,
        'dsn': DSN,
        'service_name': 'movies-api',
        'environment': 'test',
    }
    kwargs.update(overrides)
    sentry.init_sentry(**kwargs)


def _is_active() -> bool:
    return sentry_sdk.get_client().is_active()


def test_disabled_does_not_create_client():
    _init(enabled=False)
    assert not _is_active()


def test_empty_dsn_does_not_create_client_and_warns(caplog):
    with caplog.at_level(logging.WARNING, logger='practix_core.sentry'):
        _init(dsn='')
    assert not _is_active()
    # Тихий no-op — та самая ловушка «включено, а событий нет».
    assert any('SENTRY_DSN is empty' in record.message for record in caplog.records)


def test_is_idempotent():
    _init()
    first = sentry_sdk.get_client()
    _init(service_name='other')
    assert sentry_sdk.get_client() is first


def test_init_does_not_replace_global_tracer_provider():
    """ИНВАРИАНТ: трассировку ведёт Jaeger, Sentry её не перехватывает.

    ``sentry_sdk`` ставит свой глобальный провайдер, только если попросить
    трассировку. Тест фиксирует, что ``init_sentry`` не просит.
    """
    before = trace.get_tracer_provider()
    _init()
    assert trace.get_tracer_provider() is before


def test_tracing_is_not_enabled():
    """``traces_sample_rate`` должен ОТСУТСТВОВАТЬ, а не быть нулём.

    Ноль по докстрингу SDK продолжает входящие трейсы, то есть трассировка
    осталась бы включённой.
    """
    _init()
    options = sentry_sdk.get_client().options
    assert options['traces_sample_rate'] is None
    assert options['traces_sampler'] is None


def test_service_name_lands_in_server_name():
    _init(service_name='etl-clickhouse')
    assert sentry_sdk.get_client().options['server_name'] == 'etl-clickhouse'


def test_pii_is_off_by_default():
    _init()
    assert not sentry_sdk.get_client().options['send_default_pii']


def test_logging_integration_uses_given_levels():
    _init(event_level=logging.CRITICAL, breadcrumb_level=logging.WARNING)
    integration = sentry_sdk.get_client().get_integration('logging')
    assert integration is not None
    assert integration._handler.level == logging.CRITICAL
    assert integration._breadcrumb_handler.level == logging.WARNING


def test_extra_denylist_reaches_the_scrubber():
    _init()
    denylist = sentry_sdk.get_client().options['event_scrubber'].denylist
    assert 'authjwt_secret_key' in denylist
    assert 'ugc_ip_hash_salt' in denylist
    # Штатный список не должен потеряться при расширении своим.
    assert 'password' in denylist


def test_scrubber_removes_sensitive_local_variables():
    scrubber = sentry.EventScrubber(denylist=[*sentry.DEFAULT_DENYLIST, *sentry.EXTRA_DENYLIST])
    event = {
        'extra': {'password': 'hunter2', 'authjwt_secret_key': 's3cret', 'film_id': '42'},
    }
    scrubber.scrub_event(event)
    assert event['extra']['password'] != 'hunter2'
    assert event['extra']['authjwt_secret_key'] != 's3cret'
    assert event['extra']['film_id'] == '42'


# --- Процессор событий: связь с логами и трейсами ----------------------------


def test_processor_adds_static_service_tag():
    processor = sentry.build_event_processor(static_tags={'service': 'ugc-api'})
    event = processor({}, {})
    assert event['tags']['service'] == 'ugc-api'


def test_processor_adds_request_id_from_context():
    token = request_id_ctx.set('req-123')
    try:
        event = sentry.build_event_processor()({}, {})
    finally:
        request_id_ctx.reset(token)
    assert event['tags']['request_id'] == 'req-123'


def test_processor_omits_request_id_outside_a_request():
    """У ETL нет HTTP-запроса — тег не должен появляться пустым."""
    event = sentry.build_event_processor()({}, {})
    assert 'request_id' not in event['tags']


def test_processor_adds_trace_id_of_active_span():
    """Ради этого тега всё и затевалось: из ошибки — в трейс Jaeger."""
    provider = TracerProvider()
    tracer = provider.get_tracer(__name__)
    with tracer.start_as_current_span('unit') as span:
        expected = format(span.get_span_context().trace_id, '032x')
        event = sentry.build_event_processor()({}, {})
    assert event['tags']['trace_id'] == expected
    assert len(event['tags']['trace_id']) == 32


def test_processor_preserves_existing_tags():
    event = sentry.build_event_processor(static_tags={'service': 's'})({'tags': {'custom': 'kept'}}, {})
    assert event['tags']['custom'] == 'kept'
    assert event['tags']['service'] == 's'


def test_otel_trace_id_is_none_without_active_span():
    assert sentry.otel_trace_id() is None


def test_otel_trace_id_survives_missing_opentelemetry(monkeypatch):
    """Образ etl-elasticsearch собирается без OpenTelemetry — это не ошибка."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith('opentelemetry'):
            raise ImportError('no opentelemetry in this image')
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', fake_import)
    assert sentry.otel_trace_id() is None
