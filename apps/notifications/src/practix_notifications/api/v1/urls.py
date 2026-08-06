from django.urls import path

from practix_notifications.api.v1 import views

app_name = 'notifications-api'

urlpatterns = [
    # Межсервисные ручки: заявка на рассылку и заявка на одно сообщение.
    path('events', views.enqueue_event, name='events'),
    path('messages', views.enqueue_message, name='messages'),
    # Пользовательские: отписка по подписанной ссылке и лента кабинета.
    path('unsubscribe/<str:token>', views.unsubscribe, name='unsubscribe'),
    path('me/messages', views.my_messages, name='my-messages'),
    path('me/messages/<uuid:message_id>/read', views.mark_message_read, name='my-message-read'),
]
