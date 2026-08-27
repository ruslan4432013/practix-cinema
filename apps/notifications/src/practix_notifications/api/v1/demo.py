"""Демо-страница личного кабинета — витрина деградации транспорта.

Страница нужна не для красоты: политика перехода websocket → long polling →
лента живёт НА КЛИЕНТЕ, и без клиента её негде ни описать, ни проверить руками.
Теория формулирует требование именно так: «если при проблемах с Websocket
переключать пользователя на Long polling, то пользователь продолжит получать
уведомления, несмотря на сбой системы, пусть и менее эффективным способом».

## Почему страница здесь, а не в шлюзе

Она должна быть ОДНОГО ORIGIN с лентой: последняя ступень деградации — это
``GET /api/v1/notifications/me/messages`` в этом же сервисе. Иначе к
WSGI-панели пришлось бы прикручивать CORS, которого у неё нет и который стоил бы
собственного middleware. У шлюза же ``CORSMiddleware`` встроен в FastAPI и стоит
четыре строки — поэтому кросс-доменными оказываются ровно две ручки шлюза, а не
вся лента.

Логина на странице нет: токен вставляется руками. Форма входа означала бы либо
CORS к Auth, либо прокси-ручку — и то и другое ради учебного стенда, на котором
токен и так получают одной командой ``curl``.
"""

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from practix_notifications.core.config import settings


@require_GET
def cabinet_demo(request: HttpRequest) -> HttpResponse:
    if not settings.NOTIFY_CABINET_DEMO_ENABLED:
        return HttpResponse('Демо-страница выключена', status=404, content_type='text/plain; charset=utf-8')
    return render(
        request,
        'demo/cabinet.html',
        {
            # Адрес шлюза приходит из настроек, а не зашит в JavaScript: порт
            # публикуется переменной, и на чужом стенде он другой.
            'gateway_url': settings.NOTIFY_CABINET_WS_URL.rstrip('/'),
        },
    )
