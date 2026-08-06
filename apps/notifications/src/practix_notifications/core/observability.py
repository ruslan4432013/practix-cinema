"""Трассировка и сбор ошибок.

Django-специфичного здесь ровно две строки — инструментаторы; всё остальное
делает ``practix_core``. Именно ради этого сервис живёт внутри uv-workspace:
``apps/django-admin`` лежит за границей лока и вынужден держать собственную копию
инициализации Sentry в ``example/wsgi.py`` со всеми её тонкостями (в первую
очередь — с запретом передавать ``traces_sample_rate``, иначе Sentry подменяет
глобальный ``TracerProvider`` и трассировка молча уезжает из Jaeger).

Вызывается из ``wsgi.py`` — до создания WSGI-приложения, чтобы инструментация
успела пропатчить Django, — и из команд воркера и планировщика, у которых WSGI
нет вообще.
"""

import logging

from practix_core.sentry import init_sentry
from practix_core.tracing import init_tracer_provider
from practix_notifications.core.config import settings

logger = logging.getLogger('notifications.observability')


def _instrument_django() -> None:
    from opentelemetry.instrumentation.django import DjangoInstrumentor

    DjangoInstrumentor().instrument()


def _instrument_requests() -> None:
    from opentelemetry.instrumentation.requests import RequestsInstrumentor

    RequestsInstrumentor().instrument()


def init_observability(*, instrument_django: bool = True) -> None:
    """Идемпотентно поднимает трассировку и сбор ошибок.

    :param instrument_django: воркеру и планировщику Django-инструментация не
        нужна — HTTP-запросов они не обслуживают, а патч ставит обработчики на
        сигналы фреймворка впустую. Исходящие ``requests`` инструментируются
        всегда: клиент Auth есть и у них.
    """
    instrumentors = [_instrument_requests]
    if instrument_django:
        instrumentors.insert(0, _instrument_django)

    init_tracer_provider(
        enabled=settings.OTEL_ENABLED,
        service_name=settings.OTEL_SERVICE_NAME,
        endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT,
        instrumentors=instrumentors,
    )
    init_sentry(
        enabled=settings.SENTRY_ENABLED,
        dsn=settings.SENTRY_DSN,
        service_name=settings.OTEL_SERVICE_NAME,
        environment=settings.SENTRY_ENVIRONMENT,
        release=settings.SENTRY_RELEASE or None,
        sample_rate=settings.SENTRY_SAMPLE_RATE,
        send_default_pii=settings.SENTRY_SEND_DEFAULT_PII,
    )

    for problem in settings.insecure_defaults:
        logger.warning('Insecure default in use: %s', problem)
