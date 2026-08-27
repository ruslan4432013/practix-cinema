"""Две ступени доставки: websocket и long polling.

Теория урока называет три способа узнать о новом уведомлении и расставляет их по
цене: частый опрос («самый расточительный»), long polling и websocket. Здесь
реализованы второй и третий, а первый существует и без шлюза — это лента
кабинета. Вместе они дают ту самую изящную деградацию, о которой говорит теория:
«если при проблемах с Websocket переключать пользователя на Long polling, то
пользователь продолжит получать уведомления, несмотря на сбой системы».

## Лестница целиком

1. **websocket** ``/api/v1/ws?ticket=…`` — push, задержка миллисекунды;
2. **long polling** ``/api/v1/ws/poll`` — запрос висит до первого сообщения или
   до таймаута, клиент сразу открывает следующий;
3. **лента кабинета** ``GET /api/v1/notifications/me/messages?since=…`` в сервисе
   нотификаций — единственная ДОЛГОВЕЧНАЯ ступень.

Третья ступень нужна потому, что шлюз не хранит ничего. Сообщение, пришедшее
между двумя long-poll-запросами, ему просто некуда положить: подписчика в этот
момент не существует. Компенсирует это лента, которая пишется в одной транзакции
с переводом задачи доставки в «отправлено», — поэтому клиент при КАЖДОЙ смене
транспорта догоняет её по ``since`` и дедуплицирует по ``task_id``.

## Почему websocket пускает только по ticket'у, без заголовка

Одна дверь охраняется лучше двух. Заголовок ``Authorization`` на handshake
браузеру всё равно недоступен, а для websocat и тестов лишний ``curl`` за
ticket'ом стоит одну строку. Токен при этом не попадает ни в access-лог, ни в
историю браузера ни в каком виде.
"""

import asyncio
import logging

from async_fastapi_jwt_auth import AuthJWT
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status

from practix_notifications_ws.core import redis as redis_db
from practix_notifications_ws.core.config import settings
from practix_notifications_ws.models.push import Frame
from practix_notifications_ws.services import guard, tickets
from practix_notifications_ws.services.hub import ConnectionLimitReached, Subscription
from practix_notifications_ws.services.providers import get_hub
from practix_notifications_ws.services.tickets import TicketError, TicketPayload

logger = logging.getLogger('notifications_ws.stream')

router = APIRouter()


@router.websocket('')
async def stream(websocket: WebSocket, ticket: str = Query(default='')) -> None:
    """Поток уведомлений одного пользователя.

    Отказ оформляется ``close()`` ДО ``accept()``: Starlette превращает это в
    HTTP 403 на handshake, и посторонний не получает ни одного байта данных —
    в отличие от «принять и закрыть», где соединение на мгновение существует.
    """
    if not guard.origin_allowed(websocket.headers.get('origin')):
        # Cross-Site WebSocket Hijacking: у сокета нет same-origin policy, и
        # без этой проверки чужая страница открыла бы поток к нам сама.
        logger.warning('Rejected websocket from origin %r', websocket.headers.get('origin'))
        await websocket.close(code=guard.WS_CLOSE_FORBIDDEN)
        return

    redis = await redis_db.get_client()
    if redis is None:
        await websocket.close(code=guard.WS_CLOSE_SHUTDOWN)
        return

    try:
        payload = await tickets.redeem(redis, ticket)
    except TicketError as exc:
        logger.info('Rejected websocket handshake: %s', exc)
        await websocket.close(code=guard.WS_CLOSE_UNAUTHORIZED)
        return

    # Ticket погашен, но токен за это время мог быть отозван.
    if not await guard.still_valid(redis, payload):
        await websocket.close(code=guard.WS_CLOSE_UNAUTHORIZED)
        return

    hub = get_hub()
    try:
        subscription = hub.subscribe(payload.subject)
    except ConnectionLimitReached as exc:
        logger.warning('Rejected websocket: %s', exc)
        await websocket.close(code=guard.WS_CLOSE_TOO_MANY)
        return

    # accept() и hello — ВНУТРИ try, а не перед ним. Клиент успевает уйти между
    # проверками и рукопожатием (агрессивный цикл переподключения делает это
    # регулярно), и вылетевшее оттуда исключение уносило бы управление мимо
    # finally: подписка осталась бы в реестре навсегда, а счётчики соединений
    # медленно росли бы до отказа 4429 всем подряд — до перезапуска процесса.
    try:
        await websocket.accept()
        await websocket.send_json(Frame.hello(user_id=payload.subject, ping_interval=settings.NOTIFY_WS_PING_INTERVAL))
        await _serve(websocket, subscription, payload)
    finally:
        hub.unsubscribe(subscription)


async def _serve(websocket: WebSocket, subscription: Subscription, payload: TicketPayload) -> None:
    """Три задачи, живущие ровно столько, сколько соединение.

    Первая закончившаяся определяет судьбу остальных: клиент отключился —
    отправлять некому; токен отозван — держать нечего.
    """
    jobs = {
        asyncio.create_task(_push_frames(websocket, subscription), name='ws-push'),
        asyncio.create_task(_drain_client(websocket), name='ws-drain'),
        asyncio.create_task(_revalidate(payload), name='ws-revalidate'),
    }
    done, pending = await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    for task in done:
        # Исключение у завершившейся задачи ОБЯЗАТЕЛЬНО забрать. Обрыв соединения
        # посреди отправки — штатное событие, но невостребованное исключение
        # asyncio печатает в лог как «Task exception was never retrieved», и
        # обычный уход клиента выглядел бы в логах аварией.
        if not task.cancelled() and task.exception() is not None:
            logger.debug('Websocket task %s finished with %r', task.get_name(), task.exception())

    revoked = any(task.get_name() == 'ws-revalidate' and not task.cancelled() for task in done)
    if revoked:
        logger.info('Closing websocket: token is no longer valid')
        await websocket.close(code=guard.WS_CLOSE_UNAUTHORIZED)


async def _push_frames(websocket: WebSocket, subscription: Subscription) -> None:
    """Очередь подписчика → сокет, с ping'ом по таймауту.

    Ping нужен не клиенту, а промежуточным прокси: молчащее соединение они рвут
    через минуту-две, а поток уведомлений у одного человека сильно реже.
    """
    while True:
        try:
            async with asyncio.timeout(settings.NOTIFY_WS_PING_INTERVAL):
                frame = await subscription.queue.get()
        except TimeoutError:
            await websocket.send_json({'type': 'ping'})
            continue

        dropped = subscription.take_dropped()
        if dropped:
            # Кадр о пропаже ИДЁТ ПЕРВЫМ: клиент должен узнать о дыре до того,
            # как увидит следующее сообщение и решит, что ничего не пропустил.
            await websocket.send_json(Frame.desync(dropped))
        await websocket.send_json(frame)


async def _drain_client(websocket: WebSocket) -> None:
    """Чтение входящих кадров.

    Шлюз односторонний: команд от клиента у него нет. Но читать сокет обязательно —
    иначе разрыв соединения замечается только при следующей отправке, то есть
    подписка мёртвого клиента живёт до первого уведомления.
    """
    while True:
        try:
            message = await websocket.receive_json()
        except WebSocketDisconnect:
            return
        except (ValueError, TypeError, KeyError):
            # Не JSON — молча игнорируем: разрывать из-за этого соединение не за что.
            continue
        if isinstance(message, dict) and message.get('type') == 'ping':
            await websocket.send_json(Frame.pong())


async def _revalidate(payload: TicketPayload) -> None:
    """Периодическая проверка, что токен ещё жив и не отозван.

    Единственное, что закрывает вкладку вышедшего из системы пользователя раньше,
    чем истечёт срок его токена. Завершение этой задачи = «закрывай соединение».
    """
    while True:
        await asyncio.sleep(settings.NOTIFY_WS_REVALIDATE_SECONDS)
        if not await guard.still_valid(await redis_db.get_client(), payload):
            return


@router.get(
    '/poll',
    summary='Long polling — ступень деградации, когда websocket не поднялся',
    description=(
        'Запрос висит до первого уведомления или до `wait` секунд, после чего возвращает '
        'пустой список — клиент немедленно открывает следующий. Это «второй способ» из теории: '
        'холостых запросов на порядок меньше, чем у частого опроса.\n\n'
        '**Долговечности здесь нет.** Уведомление, пришедшее между двумя запросами, шлюзу '
        'некуда положить. Его забирает лента кабинета `GET /api/v1/notifications/me/messages`, '
        'поэтому при каждой смене транспорта клиент обязан догнать её по `since` и '
        'дедуплицировать по `task_id`.'
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {'description': 'Токен отсутствует, недействителен или отозван'},
        status.HTTP_429_TOO_MANY_REQUESTS: {
            'description': (
                'Исчерпан бюджет одновременных long-poll-запросов (свой, не общий с сокетами). '
                'Повторять не раньше `Retry-After`; лента кабинета доступна всегда.'
            )
        },
    },
)
async def poll(
    wait: float | None = Query(default=None, description='Сколько держать запрос, секунды'),
    authorize: AuthJWT = Depends(),
) -> dict:
    """В отличие от websocket, здесь обычный ``Authorization: Bearer`` — ticket не нужен."""
    await authorize.jwt_required()
    raw_jwt = await authorize.get_raw_jwt() or {}
    subject = str(raw_jwt.get('sub') or '')
    if not subject:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Токен без субъекта')

    timeout = settings.NOTIFY_WS_POLL_TIMEOUT if wait is None else wait
    timeout = max(1.0, min(timeout, settings.NOTIFY_WS_POLL_MAX_TIMEOUT))

    hub = get_hub()
    # Бюджет сокетов здесь не тратится: клиент, дошедший до long polling'а, УЖЕ
    # деградировал, и отказ ради чужого сокета отправил бы его на ленту. Но и
    # безлимитным он быть не может — каждый висящий запрос держит задачу и
    # очередь кадров до полуминуты, так что у поллеров свой потолок, и отказ по
    # нему не отнимает ничего у недеградировавших.
    try:
        subscription = hub.subscribe(subject, count_towards_limits=False)
    except ConnectionLimitReached as exc:
        logger.warning('Rejected long-poll: %s', exc)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Слишком много одновременных long-poll-запросов',
            # Ровно столько, сколько длился бы обычный запрос: к этому моменту
            # слот освободится, а до тех пор клиенту есть куда пойти — лента.
            headers={'Retry-After': str(max(1, int(settings.NOTIFY_WS_POLL_TIMEOUT)))},
        ) from exc
    items: list[dict] = []
    try:
        try:
            async with asyncio.timeout(timeout):
                items.append(await subscription.queue.get())
        except TimeoutError:
            pass
        # Всё, что успело накопиться, уезжает этим же ответом: открывать
        # отдельный запрос на каждое сообщение из одной пачки незачем.
        while not subscription.queue.empty():
            items.append(subscription.queue.get_nowait())
    finally:
        hub.unsubscribe(subscription)

    dropped = subscription.take_dropped()
    if dropped:
        items.insert(0, Frame.desync(dropped))
    return {'items': items, 'transport': 'long-poll', 'waited': timeout}
