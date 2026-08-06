"""Точка входа websocket-шлюза мгновенных уведомлений.

## Почему это отдельный сервис, а не ручка в панели нотификаций

Теория формулирует ограничение прямо: «Websocket стоит использовать только с
асинхронными инструментами. Многопоточный сервер рассчитан на быструю обработку
запроса и закрытие соединения, а каждое постоянно открытое соединение
заблокирует поток выполнения». Панель нотификаций — Django на gunicorn с двумя
синхронными воркерами: сто открытых вкладок заняли бы все потоки, и панель
перестала бы отвечать раньше, чем кто-нибудь получил бы первое уведомление.
Перевод панели на ASGI означал бы django-channels, daphne и вторую модель
запуска внутри одного образа — ради ручки, которая не разделяет с панелью ни
одной строчки бизнес-логики.

Поэтому — свой процесс, свой образ, свой контейнер. Ровно то же разделение, что
у формирующего и отправляющего воркеров: разные узкие места масштабируются
независимо.

## Что шлюз НЕ делает

Не имеет базы. Не решает, кого уведомлять. Не пишет ленту. Он только раздаёт по
открытым соединениям то, что воркер нотификаций уже доставил и уже записал в
``inbox_message``. Долговечность живёт там, шлюз — быстрый путь, и его падение
не теряет ни одного уведомления.

## Nginx перед шлюзом нет — намеренно

Три причины, и каждой хватило бы. Nginx резолвит upstream'ы на старте и падает с
``host not found in upstream``, если профильного контейнера нет, — маршрут к
шлюзу сломал бы ядро стенда всем, кто не поднимает профиль ``notifications``.
Глобальный ``proxy_set_header Connection ""`` (он нужен для keepalive к
upstream'ам) прямо ломает handshake, а ``proxy_read_timeout 5s`` рвал бы
соединение каждые пять секунд: понадобились бы ``map $http_upgrade`` и отдельная
location со своими таймаутами. И ровно по первой причине панель нотификаций тоже
публикуется прямым хост-портом. Следствие: браузер не присылает ``X-Request-Id``,
поэтому middleware работает в режиме «сгенерировать, если нет».
"""

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from practix_core.jwt import (
    install_config_loader,
    install_denylist_loader,
    install_exception_handler,
    make_jwt_settings,
)
from practix_core.openapi import install_bearer_security
from practix_core.request_id import RequestIdMiddleware
from practix_notifications_ws.api.v1 import health, stream, tickets
from practix_notifications_ws.brokers.consumer import PushConsumer
from practix_notifications_ws.core import redis as redis_db
from practix_notifications_ws.core.config import settings
from practix_notifications_ws.core.observability import LOGGING, init_runtime_observability, setup_logging
from practix_notifications_ws.services import providers
from practix_notifications_ws.services.hub import ConnectionHub

setup_logging()
logger = logging.getLogger('notifications_ws.main')


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_runtime_observability(app)

    providers.hub = ConnectionHub(
        queue_size=settings.NOTIFY_WS_QUEUE_SIZE,
        max_per_user=settings.NOTIFY_WS_MAX_CONNECTIONS_PER_USER,
        max_total=settings.NOTIFY_WS_MAX_CONNECTIONS,
    )
    redis_db.client = redis_db.create_client()
    if not await redis_db.reachable(redis_db.client):
        # Соединение у redis-py ленивое: конструктор выше не проверяет ничего,
        # и опечатка в AUTH_REDIS_HOST молчала бы до первого handshake, вылезая
        # поголовными отказами. Старт при этом НЕ срывается: экземпляр Redis
        # общий на все реплики, и падение здесь превратило бы его моргание в
        # лавину рестартов. Это degraded, а не фатально.
        logger.warning(
            'REDIS UNREACHABLE: %s:%s (база %s) не отвечает — пока это так, ticket’ы не выдаются '
            'и новые websocket-подключения невозможны',
            settings.AUTH_REDIS_HOST,
            settings.AUTH_REDIS_PORT,
            settings.AUTH_REDIS_DB,
        )

    providers.consumer = PushConsumer(providers.hub)
    providers.consumer.start()
    yield
    if providers.consumer is not None:
        await providers.consumer.stop()
        providers.consumer = None
    if redis_db.client is not None:
        await redis_db.client.aclose()
        redis_db.client = None
    providers.hub = None


app = FastAPI(
    title='Websocket-шлюз уведомлений',
    description=(
        'Мгновенная доставка уведомлений в открытую вкладку с изящной деградацией.\n\n'
        '**Лестница транспортов:** websocket → long polling → лента кабинета. Первые две ступени '
        'здесь, третья — в сервисе нотификаций (`GET /api/v1/notifications/me/messages`). '
        'Долговечна только третья: шлюз не хранит ничего и его падение не теряет уведомлений.\n\n'
        '**Авторизация.** Токен на handshake не передаётся: браузер не умеет ставить заголовки в '
        '`new WebSocket()`, а JWT в query-строке навсегда оседает в access-логах. Вместо него — '
        'одноразовый ticket, который выдаётся по `POST /api/v1/ws/ticket` и гаснет при первом '
        'использовании. Соединение периодически перепроверяется по денилисту Auth, поэтому '
        'выход из системы закрывает открытые вкладки.'
    ),
    version='1.0.0',
    # `/api/openapi` за nginx занят Movies API, `/api/analytics/openapi` —
    # коллектором, `/api/ugc/openapi` — UGC API.
    docs_url='/api/ws/openapi',
    openapi_url='/api/ws/openapi.json',
    lifespan=lifespan,
)

# 'generate', а не 'reject_400': шлюз публикуется прямым хост-портом, nginx
# заголовок не проставит, а браузер его не пришлёт — режим отказа означал бы
# 400 на каждый handshake. То же решение, что в панели нотификаций.
app.add_middleware(RequestIdMiddleware, on_missing='generate')

# CORS нужен потому, что демо-страница живёт на порту панели (8090), а ручки
# ticket'а и long polling'а — здесь (8091). Сам websocket под CORS не подпадает
# вовсе — его прикрывает проверка Origin на handshake (services/guard.py).
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=False,
    allow_methods=['GET', 'POST', 'OPTIONS'],
    allow_headers=['Authorization', 'Content-Type', 'X-Request-Id'],
    expose_headers=['X-Request-Id'],
)

# Секрет общий с Auth: токены здесь только проверяются и никогда не выпускаются,
# поэтому сетевых обращений к Auth на горячем пути нет.
#
# on_error='deny' — как в личном кабинете и UGC API. Открыть поток уведомлений
# по токену, про который неизвестно, жив ли он, хуже, чем не открыть никому.
install_config_loader(
    lambda: make_jwt_settings(
        authjwt_secret_key=settings.AUTHJWT_SECRET_KEY,
        authjwt_denylist_enabled=settings.AUTHJWT_DENYLIST_ENABLED,
        authjwt_denylist_token_checks=settings.AUTHJWT_DENYLIST_TOKEN_CHECKS,
    )
)
install_denylist_loader(redis_db.get_client, on_error=settings.NOTIFY_WS_DENYLIST_ON_ERROR)  # type: ignore[arg-type]
install_exception_handler(app)

app.include_router(tickets.router, prefix='/api/v1/ws', tags=['Подключение'])
app.include_router(stream.router, prefix='/api/v1/ws', tags=['Доставка'])
app.include_router(health.router, prefix='/health', tags=['Служебные'])

install_bearer_security(app)


def run() -> None:
    """Точка входа для локальной отладки."""
    uvicorn.run('practix_notifications_ws.main:app', host='0.0.0.0', port=8000, log_config=LOGGING)


if __name__ == '__main__':
    run()
