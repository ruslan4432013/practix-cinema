"""
WSGI config for example project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/4.0/howto/deployment/wsgi/
"""

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


_init_tracing()

application = get_wsgi_application()
