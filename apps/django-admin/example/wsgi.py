"""
WSGI config for example project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/4.0/howto/deployment/wsgi/
"""

import logging
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'example.settings')


def _init_tracing() -> None:
    """Инициализация OpenTelemetry-трассировки (OTLP/HTTP -> Jaeger).

    Вызывается из wsgi.py, поэтому при ``lazy-apps = true`` в uWSGI выполняется
    уже после fork'а каждого воркера — фоновый поток экспорта span'ов безопасен.
    manage.py/миграции wsgi.py не импортируют, поэтому остаются без трассировки.
    """
    from example.config import settings

    if not settings.OTEL_ENABLED:
        return
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.django import DjangoInstrumentor
    from opentelemetry.instrumentation.requests import RequestsInstrumentor
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    endpoint = settings.OTEL_EXPORTER_OTLP_ENDPOINT.rstrip('/')
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: settings.OTEL_SERVICE_NAME}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f'{endpoint}/v1/traces')))
    trace.set_tracer_provider(provider)
    DjangoInstrumentor().instrument()  # серверные span'ы + извлечение traceparent
    RequestsInstrumentor().instrument()  # исходящие вызовы в Auth (auth_backend.py)


def _init_sentry() -> None:
    """Инициализация сбора ошибок (Sentry / GlitchTip).

    ФОРК, а не переиспользование ``practix_core.sentry``, и это та же граница,
    из-за которой у админки форкнут RequestId-middleware: проект вне uv
    workspace (Python 3.12, свой ``uv.lock``), библиотеки монорепозитория ему
    недоступны. Двадцать строк здесь дешевле, чем тянуть practix-core через
    границу лока.

    Инвариант общий с библиотекой и здесь тоже обязателен: ``traces_sample_rate``
    НЕ ПЕРЕДАЁТСЯ. Трассировку ведёт Jaeger — провайдер поставлен в
    ``_init_tracing()`` выше, и интеграция Sentry с OpenTelemetry подменила бы
    его своим, тихо уведя span'ы из Jaeger.

    Как и трассировка, вызывается из wsgi.py: при ``lazy-apps = true`` это
    происходит после fork'а каждого воркера, а ``enable-threads = true`` в
    uwsgi.ini даёт SDK поднять фоновый поток отправки.
    """
    from example.config import settings

    if not settings.SENTRY_ENABLED:
        return
    if not settings.SENTRY_DSN:
        # Тихий no-op — ловушка «включено, а событий нет».
        logging.getLogger(__name__).warning('Sentry is enabled but SENTRY_DSN is empty — error reporting is off')
        return

    import sentry_sdk

    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.SENTRY_ENVIRONMENT,
        release=settings.SENTRY_RELEASE or None,
        server_name=settings.OTEL_SERVICE_NAME,
        sample_rate=settings.SENTRY_SAMPLE_RATE,
        send_default_pii=settings.SENTRY_SEND_DEFAULT_PII,
        shutdown_timeout=2,
    )
    sentry_sdk.set_tag('service', settings.OTEL_SERVICE_NAME)

    from opentelemetry import trace

    def add_trace_id(event, _hint):
        """Тег для перехода из ошибки в конкретный трейс Jaeger."""
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            event.setdefault('tags', {})['trace_id'] = format(span_context.trace_id, '032x')
        return event

    sentry_sdk.get_global_scope().add_event_processor(add_trace_id)


_init_tracing()
_init_sentry()

application = get_wsgi_application()
