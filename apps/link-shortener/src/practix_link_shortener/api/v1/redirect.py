"""Публичный маршрут короткой ссылки: ``GET|HEAD /s/{code}``.

Единственная ручка сервиса, которую открывает человек. Отсюда всё остальное:
ответы — HTML, а не JSON; ошибки — страницы, а не ``detail``; заголовок
``X-Request-Id`` не требуется (см. ``core/request_id.py``).
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from practix_link_shortener.api.v1.dependencies import (
    get_auth_client,
    get_link_service,
    get_session_factory,
)
from practix_link_shortener.core.config import settings
from practix_link_shortener.core.templates import templates
from practix_link_shortener.services.auth_client import AuthClient
from practix_link_shortener.services.exceptions import (
    ConfirmMisconfigured,
    ConfirmRejected,
    ConfirmUnavailable,
)
from practix_link_shortener.services.link_service import LinkService, needs_confirmation

logger = logging.getLogger(__name__)

router = APIRouter()

#: 302, а НЕ 301. Три довода по убыванию веса:
#: 1. 301 кэшируется браузером и промежуточными узлами навсегда. Отозванная или
#:    протухшая ссылка продолжала бы уводить из кэша — прямое противоречие
#:    требованию «по просроченной ссылке открывается 404».
#: 2. 301 замораживает целевой адрес: смена `redirectUrl` в панели не повлияла бы
#:    на уже разосланные письма.
#: 3. Визиты по закэшированному редиректу не считаются, а считать их — половина
#:    задания.
#: Теория называет допустимыми оба кода, так что выбор наш.
REDIRECT_STATUS = 302

#: Ни редирект, ни страница ошибки не должны оседать в кэше — см. довод 1 выше.
NO_STORE = {'Cache-Control': 'no-store'}


@router.api_route('/{code}', methods=['GET', 'HEAD'], include_in_schema=False)
async def follow(
    code: str,
    request: Request,
    background: BackgroundTasks,
    service: LinkService = Depends(get_link_service),
    auth: AuthClient = Depends(get_auth_client),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
):
    link = await service.resolve(code)
    if link is None:
        # «Нет такой», «протухла» и «отозвана» — один ответ намеренно: разные
        # ответы стали бы оракулом, подсказывающим перебирающему, какие коды
        # существуют.
        return _page(request, '404.html', 404)

    # HEAD не подтверждает адрес и не считается визитом — по одной и той же
    # причине: так ходят сканеры ссылок почтовых клиентов, а не человек.
    # Подтвердить почту по запросу сканера значило бы отдать смысл ссылки
    # почтовому провайдеру: адрес оказался бы «подтверждён» до того, как письмо
    # вообще открыли. Живость ссылки при этом проверяется как обычно, поэтому
    # HEAD по-прежнему честно отвечает 404 на протухшую и 302 на живую.
    is_probe = request.method == 'HEAD'

    if needs_confirmation(link) and not is_probe:
        try:
            outcome = await auth.confirm_email(link.user_id)
        except ConfirmRejected:
            # Пользователя больше нет — ссылка указывает в пустоту.
            logger.info('Подтверждение отклонено: Auth не знает пользователя %s', link.user_id)
            return _page(request, '404.html', 404)
        except ConfirmMisconfigured:
            # Не заведена служебная учётка, отобрали роль, не тот пароль. Это
            # проблема оператора, а не человека с письмом, — поэтому ERROR в лог
            # и 503 ему, а не 404 («ваша ссылка протухла» было бы ложью).
            logger.exception('Подтверждение невозможно из-за конфигурации служебной учётки')
            return _page(request, '503.html', 503, headers={'Retry-After': '60'})
        except ConfirmUnavailable:
            logger.warning('Auth недоступен, подтверждение отложено: код %s', code)
            return _page(request, '503.html', 503, headers={'Retry-After': '60'})
        logger.info('Адрес пользователя %s: %s', link.user_id, outcome)

    # Счётчик — ПОСЛЕ решения и в фоне: медленный счётчик не должен задерживать
    # редирект, а упавший — отменять уже случившееся подтверждение.
    #
    # GET-префетч это не ловит — от него спасает только идемпотентность
    # подтверждения.
    if not is_probe:
        background.add_task(_count_visit, session_factory, code)

    return RedirectResponse(link.target_url, status_code=REDIRECT_STATUS, headers=NO_STORE)


async def _count_visit(session_factory: async_sessionmaker[AsyncSession], code: str) -> None:
    """Учесть переход в отдельной сессии.

    Своя сессия, а не та, что пришла зависимостью: к моменту выполнения фоновой
    задачи FastAPI уже закрыл сессию запроса. Фабрика приходит зависимостью, а не
    импортом из ``db.postgres``: иначе её нечем подменить в тестах.
    """
    try:
        async with session_factory() as session:
            await LinkService(session).count_visit(code)
    except Exception:  # noqa: BLE001 — фоновой задаче некому передать исключение
        # Потерянный визит — это неточная статистика. Исключение, вылетевшее из
        # фоновой задачи, ничего бы не починило, зато засорило бы лог трейсом
        # там, где хватает одной строки.
        logger.warning('Не удалось учесть визит по коду %s', code, exc_info=True)


def _page(request: Request, template: str, status_code: int, headers: dict | None = None) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name=template,
        context={'site_url': settings.SHORTENER_PUBLIC_SITE_URL},
        status_code=status_code,
        headers={**NO_STORE, **(headers or {})},
    )
