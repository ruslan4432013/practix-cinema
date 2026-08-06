"""Маршруты сервиса.

``/admin/`` — панель менеджера, ``/api/v1/notifications/`` — приём событий
извне, ``/demo/cabinet`` — витрина деградации транспорта, ``/health/*`` — пробы.
Nginx перед сервисом нет (он живёт в compose-профиле и публикуется прямым
хост-портом), поэтому конфликтовать с ``location /admin`` из
``infra/nginx/configs/site.conf`` эти пути не могут.
"""

from django.contrib import admin
from django.urls import include, path

from practix_notifications.api.v1 import demo
from practix_notifications.api.v1 import views as health_views

admin.site.site_header = 'Practix — рассылки'
admin.site.site_title = 'Practix Notifications'
admin.site.index_title = 'Управление рассылками'

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/v1/notifications/', include('practix_notifications.api.v1.urls')),
    # Витрина деградации транспорта. Здесь, а не в websocket-шлюзе: страница
    # обязана быть одного origin с лентой — та и есть последняя ступень.
    path('demo/cabinet', demo.cabinet_demo, name='cabinet-demo'),
    path('health/live', health_views.health_live, name='health-live'),
    path('health/ready', health_views.health_ready, name='health-ready'),
]
