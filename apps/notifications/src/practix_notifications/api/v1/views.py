"""HTTP-интерфейс: пробы, приём событий, отписка.

Обычные функции-вьюхи и ``JsonResponse``, без DRF, — домашний стиль репозитория
(``apps/django-admin/movies/api/v1/views.py`` устроен так же). Ручек три, и
сериализаторы с роутерами обошлись бы дороже, чем стоят.

Приём событий существует, чтобы источником рассылки могла быть не только кнопка
в админке: теория перечисляет три источника — любая часть сайта, генератор
автоматических событий и админ-панель, — и все три обязаны приходить в одну точку.
"""

import hmac
import json
import logging
from datetime import UTC, datetime
from functools import wraps

from django.core import signing
from django.db import DatabaseError, connection
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from practix_notifications.campaigns.models import OutboxMessage
from practix_notifications.core.config import settings
from practix_notifications.core.jwt_auth import TokenClaims, TokenError, authenticate
from practix_notifications.inbox.models import InboxMessage
from practix_notifications.services import intake
from practix_notifications.services.intake import IntakeResult
from practix_notifications.services.links import read_token
from practix_notifications.subscribers.models import ChannelOptout, Subscriber

logger = logging.getLogger('notifications.api')

INTERNAL_TOKEN_HEADER = 'X-Internal-Token'


@require_GET
def health_live(request: HttpRequest) -> JsonResponse:
    """Жив ли процесс. Зависимостей не трогает намеренно.

    Перезапуск контейнера не чинит упавший Postgres, а падающая проба живости
    заставила бы Docker перезапускать здоровый процесс по кругу.
    """
    return JsonResponse({'status': 'ok'})


@require_GET
def health_ready(request: HttpRequest) -> JsonResponse:
    """Готов ли сервис обслуживать запросы."""
    checks = {'database': 'ok'}
    status = 200
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
    except DatabaseError as exc:
        checks['database'] = f'error: {exc}'
        status = 503

    degraded = False
    if status == 200:
        # Растущий необработанный outbox означает, что планировщик не работает
        # или брокер недоступен: сервис отвечает, но рассылки стоят.
        unpublished = OutboxMessage.objects.filter(published_at__isnull=True)
        pending = unpublished.count()
        # Строки, исчерпавшие потолок попыток, — это уже не «ещё не дошли руки»,
        # а «доставить некуда». Отдельным числом, потому что чинятся они
        # по-разному: первое ждёт брокера, второе ждёт человека.
        stuck = unpublished.filter(attempts__gte=settings.NOTIFY_OUTBOX_MAX_ATTEMPTS).count()
        checks['outbox_pending'] = pending
        checks['outbox_stuck'] = stuck
        # 200, а не 503: выбивать панель из балансировки из-за неработающего
        # планировщика неправильно — админка и приём заявок исправны, встал
        # только слив. Проба говорит «degraded», алерт смотрит на поле.
        degraded = bool(stuck) or pending >= settings.NOTIFY_OUTBOX_ALERT_PENDING
    return JsonResponse(
        {'status': 'ok' if status == 200 and not degraded else 'degraded', 'checks': checks}, status=status
    )


def internal_token_required(view):
    """Аутентификация межсервисной ручки общим секретом.

    Декоратор, а не проверка в каждой вьюхе: ручек с этим заголовком уже две, и
    третья копия двенадцати строк — это ровно тот клон, который ловит порог
    дублирования в CI.
    """

    @wraps(view)
    def wrapper(request: HttpRequest, *args, **kwargs) -> JsonResponse:
        # Сравниваем БАЙТЫ: compare_digest на строках требует, чтобы обе были из
        # ASCII, и на присланной кириллице падает TypeError — то есть неверный
        # токен давал бы 500 вместо 401.
        provided = request.headers.get(INTERNAL_TOKEN_HEADER, '').encode('utf-8')
        if not hmac.compare_digest(provided, settings.NOTIFY_INTAKE_TOKEN.encode('utf-8')):
            return JsonResponse({'detail': 'invalid internal token'}, status=401)
        return view(request, *args, **kwargs)

    return wrapper


def json_body(view):
    """Разбор тела запроса. Вьюха получает готовый словарь вторым аргументом."""

    @wraps(view)
    def wrapper(request: HttpRequest, *args, **kwargs) -> JsonResponse:
        try:
            payload = json.loads(request.body.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return JsonResponse({'detail': 'body must be valid JSON'}, status=400)
        if not isinstance(payload, dict):
            return JsonResponse({'detail': 'body must be a JSON object'}, status=400)
        return view(request, payload, *args, **kwargs)

    return wrapper


def _respond(result: IntakeResult) -> JsonResponse:
    return JsonResponse(result.body, status=result.status)


@csrf_exempt
@require_POST
@internal_token_required
@json_body
def enqueue_event(request: HttpRequest, payload: dict) -> JsonResponse:
    """Приём заявки извне: фиксированное событие или запуск готовой рассылки.

    CSRF снят осознанно: ручка межсервисная, сессии у неё нет, а аутентификация
    сделана общим секретом в заголовке.

    Две формы тела различаются по наличию ``type``. Порядок ветвления выбран так,
    чтобы исходный контракт (``{"campaign_id": ...}``) остался буквально прежним,
    включая 400 на пустом теле.
    """
    # Ветвление по НАЛИЧИЮ ключа, а не по его истинности: `{"type": ""}` — это
    # событие с пустым именем, и вызывающий обязан узнать про пустое имя, а не
    # получить «нужен campaign_id» про поле, которого он не собирался слать.
    if 'type' in payload:
        return _respond(intake.handle_domain_event(payload))
    if payload.get('campaign_id'):
        return _respond(intake.handle_campaign_launch(payload))
    return JsonResponse({'detail': 'campaign_id or type is required'}, status=400)


@csrf_exempt
@require_POST
@internal_token_required
@json_body
def enqueue_message(request: HttpRequest, payload: dict) -> JsonResponse:
    """Сообщение в свободном формате конкретному пользователю."""
    return _respond(intake.handle_direct_message(payload))


def _authenticated(view):
    """Пользовательская аутентификация: свой токен, свои уведомления.

    Идентификатор берётся ТОЛЬКО из subject токена и никогда из query или тела —
    то же правило, что в UGC API и коллекторе. Иначе ленту чужого человека можно
    было бы прочитать, подставив его UUID.
    """

    @wraps(view)
    def wrapper(request: HttpRequest, *args, **kwargs) -> JsonResponse:
        try:
            claims = authenticate(request)
        except TokenError as exc:
            return JsonResponse({'detail': str(exc)}, status=401)
        return view(request, claims, *args, **kwargs)

    return wrapper


@require_GET
@_authenticated
def my_messages(request: HttpRequest, claims: TokenClaims) -> JsonResponse:
    """Последние уведомления пользователя для личного кабинета.

    Подписчика, которому ещё ничего не отправляли, здесь нет — и это пустой 200,
    а не 404: у аутентифицированного человека лента существует всегда, просто она
    может быть пуста.

    ЭТО ЖЕ — ПОСЛЕДНЯЯ СТУПЕНЬ ДЕГРАДАЦИИ websocket-шлюза. Шлюз не хранит ничего:
    кадр, пришедший в закрытую вкладку, исчезает, а сообщение между двумя
    long-poll-запросами ему некуда положить. Догоняется это здесь — отсюда
    ``since`` (иначе клиент вычитывал бы всю историю на каждом переподключении)
    и ``task_id`` в ответе (ключ, по которому кадр из сокета склеивается со
    строкой ленты; ``InboxMessage.task`` — OneToOne, так что он стабилен).
    """
    limit, offset = _pagination(request)
    queryset = InboxMessage.objects.filter(subscriber_id=claims.subject).select_related('campaign').order_by('-sent_at')
    since = _since(request)
    if since is not None:
        queryset = queryset.filter(sent_at__gt=since)
    total = queryset.count()
    items = [
        {
            'id': str(message.id),
            'task_id': str(message.task_id),
            'subject': message.subject,
            'preview': message.preview,
            'channel': message.channel,
            'category': message.category,
            'sent_at': message.sent_at.isoformat(),
            'read_at': message.read_at.isoformat() if message.read_at else None,
            'campaign_title': message.campaign.name,
            'event_type': message.event_type,
        }
        for message in queryset[offset : offset + limit]
    ]
    # Форма ответа совпадает с UserListResponse в Auth: два пользовательских API
    # одного стенда должны листаться одинаково.
    return JsonResponse({'items': items, 'total': total, 'limit': limit, 'offset': offset})


@csrf_exempt
@require_POST
@_authenticated
def mark_message_read(request: HttpRequest, claims: TokenClaims, message_id) -> JsonResponse:
    """Отметить уведомление прочитанным."""
    updated = InboxMessage.objects.filter(pk=message_id, subscriber_id=claims.subject, read_at__isnull=True).update(
        read_at=datetime.now(UTC)
    )
    if not updated:
        # Либо чужое, либо уже прочитано, либо не существует. Различать эти три
        # случая в ответе значило бы подтверждать существование чужих записей.
        exists = InboxMessage.objects.filter(pk=message_id, subscriber_id=claims.subject).exists()
        if not exists:
            return JsonResponse({'detail': 'message not found'}, status=404)
    return JsonResponse({'status': 'ok'})


def _pagination(request: HttpRequest) -> tuple[int, int]:
    """Разбор limit/offset. Шесть строк на месте вместо зависимости.

    ``practix_core.pagination.PaginationParams`` построен на ``Query`` из FastAPI
    и в WSGI-обработчике не работает.
    """
    try:
        limit = int(request.GET.get('limit', settings.NOTIFY_CABINET_PAGE_SIZE))
        offset = int(request.GET.get('offset', 0))
    except ValueError:
        limit, offset = settings.NOTIFY_CABINET_PAGE_SIZE, 0
    limit = max(1, min(limit, settings.NOTIFY_CABINET_MAX_PAGE_SIZE))
    return limit, max(0, offset)


def _since(request: HttpRequest) -> datetime | None:
    """Разбор курсора догона.

    Строго БОЛЬШЕ переданного момента: клиент присылает ``sent_at`` последнего
    известного ему сообщения, и включающая граница вернула бы его же снова.

    Кривое значение — это ``None``, а не 400: ручка вызывается из кода
    восстановления после обрыва, и отказ там означал бы, что клиент, потерявший
    сокет, теряет заодно и последнюю ступень деградации. Полная лента — худший
    ответ, чем точный, но не сломанный.
    """
    raw = request.GET.get('since')
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        logger.warning('Ignoring malformed since=%r', raw[:64])
        return None
    # Наивное время трактуем как UTC: база хранит момент в UTC, и сравнение
    # naive с aware в Django — это исключение, а не «как-нибудь сравнится».
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def unsubscribe(request: HttpRequest, token: str) -> HttpResponse:
    """Отписка по ссылке из письма: GET показывает вопрос, POST отписывает.

    Подписанный токен вместо идентификатора в открытом виде: иначе, подставив
    чужой UUID, можно отписать кого угодно.

    ## Почему отписка не делается по GET

    По ссылке из письма первым ходит не человек. Outlook Safe Links, антивирусные
    шлюзы и префетчеры почтовых клиентов открывают всё, что нашли в теле, — и
    отписка по GET означала бы, что часть получателей отписывается от рассылки,
    никогда её не открыв, а менеджер видит необъяснимый обвал аудитории. Ровно
    та же причина, по которой ``HEAD`` не подтверждает адрес в сервисе коротких
    ссылок: побочный эффект вешается на метод, которым ходит человек.

    Промежуточная страница с кнопкой — это ещё и подтверждение: отписка
    необратима для менеджера и неочевидна для того, кто нажал случайно.

    ``csrf_exempt`` — потому что у получателя письма нет сессии и неоткуда взять
    токен CSRF; защитой служит сам подписанный токен в адресе, который POST
    берёт из URL, а не из тела.
    """
    try:
        payload = read_token(token)
    except signing.BadSignature:
        return HttpResponse('Ссылка недействительна или устарела', status=404, content_type='text/plain; charset=utf-8')

    subscriber = Subscriber.objects.filter(pk=payload.get('s')).first()
    if subscriber is None:
        return HttpResponse('Подписчик не найден', status=404, content_type='text/plain; charset=utf-8')

    if request.method == 'GET':
        return render(request, 'unsubscribe/confirm.html', {'email': subscriber.email, 'token': token})

    ChannelOptout.objects.get_or_create(
        subscriber=subscriber, channel=payload.get('ch', ''), category=payload.get('cat', '')
    )
    logger.info('Unsubscribed', extra={'subscriber_id': str(subscriber.id), 'channel': payload.get('ch')})
    return render(request, 'unsubscribe/done.html', {'email': subscriber.email})
