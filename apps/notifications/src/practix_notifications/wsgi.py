"""WSGI-приложение панели: ``gunicorn practix_notifications.wsgi:application``.

Наблюдаемость поднимается ДО ``get_wsgi_application()``: инструментация
OpenTelemetry патчит Django на импорте, и после создания приложения патч уже не
успеет обернуть обработчик запроса.
"""

import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'practix_notifications.settings')

from django.core.wsgi import get_wsgi_application

from practix_notifications.core.observability import init_observability

init_observability(instrument_django=True)

application = get_wsgi_application()
