"""Проверки извлечённой инициализации трассировки.

Опасность этого кластера не в том, что трассировка «не заведётся» — это видно
сразу. Опасны два тихих регресса: (1) сэмплирование, случайно включённое там, где
его не было, обрежет часть трейсов Movies API, и никто этого не заметит;
(2) потерянный ``ParentBased`` у ETL порвёт трейс посередине, оставив спаны
коллектора без продолжения.
"""

import pytest
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from practix_core import tracing


@pytest.fixture(autouse=True)
def _reset():
    tracing.reset_for_testing()
    yield
    tracing.reset_for_testing()


class _NoopProcessor:
    """Заглушка span-процессора.

    Нужны именно эти методы: TracerProvider регистрирует atexit-хук и вызывает
    их при завершении интерпретатора. Голый ``object()`` давал бы поток
    AttributeError'ов уже после прогона тестов.
    """

    def on_start(self, span, parent_context=None): ...
    def on_end(self, span): ...
    def shutdown(self): ...
    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def _provider(monkeypatch) -> list:
    """Перехватывает созданный TracerProvider вместо реального экспорта."""
    created: list = []
    real_provider = tracing.TracerProvider

    def spy(*args, **kwargs):
        provider = real_provider(*args, **kwargs)
        created.append(provider)
        return provider

    monkeypatch.setattr(tracing, 'TracerProvider', spy)
    monkeypatch.setattr(tracing, 'BatchSpanProcessor', lambda exporter: _NoopProcessor())
    # Глобальный провайдер в тестах не ставим: OpenTelemetry разрешает это лишь
    # один раз на процесс и пишет предупреждение на повторные попытки.
    monkeypatch.setattr(tracing.trace, 'set_tracer_provider', lambda provider: None)
    monkeypatch.setattr(tracing, 'OTLPSpanExporter', lambda endpoint: endpoint)
    return created


def test_disabled_does_nothing(monkeypatch):
    created = _provider(monkeypatch)
    tracing.init_tracer_provider(enabled=False, service_name='svc', endpoint='http://j:4318')
    assert created == []


def test_is_idempotent(monkeypatch):
    created = _provider(monkeypatch)
    for _ in range(3):
        tracing.init_tracer_provider(enabled=True, service_name='svc', endpoint='http://j:4318')
    assert len(created) == 1


def test_no_ratio_sampler_by_default(monkeypatch):
    """rest/auth/коллектор трассируют всё — ratio-сэмплер не должен появиться.

    Провайдер по умолчанию всё равно использует ``ParentBased``, но с корневым
    ``ALWAYS_ON``; проверяем именно корневой сэмплер, а не обёртку.
    """
    created = _provider(monkeypatch)
    tracing.init_tracer_provider(enabled=True, service_name='svc', endpoint='http://j:4318')
    root = getattr(created[0].sampler, '_root', created[0].sampler)
    assert not isinstance(root, TraceIdRatioBased)
    # Дефолт SDK — ParentBased с корневым AlwaysOn, то есть трассируется всё.
    assert 'root:AlwaysOnSampler' in created[0].sampler.get_description()


def test_sampler_ratio_wraps_in_parent_based(monkeypatch):
    """У ETL обязательно ParentBased поверх ratio, иначе трейс рвётся посередине."""
    created = _provider(monkeypatch)
    tracing.init_tracer_provider(enabled=True, service_name='etl', endpoint='http://j:4318', sampler_ratio=0.05)
    sampler = created[0].sampler
    assert isinstance(sampler, ParentBased)
    root = sampler._root
    assert isinstance(root, TraceIdRatioBased)
    assert root.rate == 0.05


def test_service_name_lands_in_resource(monkeypatch):
    created = _provider(monkeypatch)
    tracing.init_tracer_provider(enabled=True, service_name='movies-api', endpoint='http://j:4318')
    assert created[0].resource.attributes['service.name'] == 'movies-api'


def test_endpoint_gets_v1_traces_suffix_and_strips_slash(monkeypatch):
    captured: list = []
    _provider(monkeypatch)
    monkeypatch.setattr(tracing, 'OTLPSpanExporter', lambda endpoint: captured.append(endpoint) or endpoint)
    tracing.init_tracer_provider(enabled=True, service_name='s', endpoint='http://jaeger:4318/')
    assert captured == ['http://jaeger:4318/v1/traces']


def test_instrumentors_run_after_provider_is_set(monkeypatch):
    order: list = []
    _provider(monkeypatch)
    monkeypatch.setattr(tracing.trace, 'set_tracer_provider', lambda p: order.append('provider'))
    tracing.init_tracer_provider(
        enabled=True,
        service_name='s',
        endpoint='http://j:4318',
        instrumentors=(lambda: order.append('instrument-a'), lambda: order.append('instrument-b')),
    )
    assert order == ['provider', 'instrument-a', 'instrument-b']


def test_instrumentors_are_not_run_when_disabled(monkeypatch):
    ran: list = []
    _provider(monkeypatch)
    tracing.init_tracer_provider(
        enabled=False, service_name='s', endpoint='http://j:4318', instrumentors=(lambda: ran.append(1),)
    )
    assert ran == []


def test_instrument_aiokafka_survives_missing_package(monkeypatch):
    """Отсутствие пакета инструментации не должно ронять сервис."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if 'aiokafka' in name:
            raise ImportError('no aiokafka instrumentation here')
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', fake_import)
    tracing.instrument_aiokafka()  # не должно бросить


def test_instrument_fastapi_is_noop_when_disabled():
    sentinel = object()
    tracing.instrument_fastapi(sentinel, enabled=False)  # не должно упасть на объекте-заглушке


def test_get_tracer_works_without_provider():
    """no-op трейсер: вызывающий код не обрастает проверками флага."""
    tracer = tracing.get_tracer('etl.clickhouse')
    with tracer.start_as_current_span('span'):
        pass
