"""Межсервисная ручка создания ссылок.

Маршрута в nginx у неё НЕТ намеренно: снаружи сети она недостижима по
построению, а не по списку разрешённых адресов, который однажды поправят не в ту
сторону. Внутри сети её закрывает служебный токен.
"""

from fastapi import APIRouter, Depends, HTTPException, Response, status

from practix_link_shortener.api.v1.dependencies import get_link_service, require_internal_token
from practix_link_shortener.models.entity import KIND_CONFIRM_EMAIL
from practix_link_shortener.models.schemas import LinkCreateRequest, LinkInfoResponse, LinkResponse
from practix_link_shortener.services.exceptions import CodeGenerationFailed, InvalidTargetUrl
from practix_link_shortener.services.link_service import LinkService, build_short_url

router = APIRouter(dependencies=[Depends(require_internal_token)])


@router.post(
    '',
    response_model=LinkResponse,
    status_code=status.HTTP_201_CREATED,
    summary='Создать короткую ссылку',
    description=(
        'Заводит короткую ссылку и возвращает её внешний адрес. Повторный вызов с тем же '
        '`idempotency_key` возвращает **200** и ТУ ЖЕ ссылку — так пересобранная пачка писем не '
        'порождает человеку вторую ссылку. `kind=confirm_email` требует `user_id`: перед '
        'редиректом такая ссылка помечает адрес подтверждённым в Auth.'
    ),
    responses={
        status.HTTP_200_OK: {'description': 'Ссылка с таким ключом идемпотентности уже была'},
        status.HTTP_400_BAD_REQUEST: {'description': 'Целевой адрес не прошёл проверку'},
        status.HTTP_401_UNAUTHORIZED: {'description': 'Неверный служебный токен'},
    },
)
async def create_link(
    payload: LinkCreateRequest,
    response: Response,
    service: LinkService = Depends(get_link_service),
):
    if payload.kind == KIND_CONFIRM_EMAIL and payload.user_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='kind=confirm_email требует user_id: подтверждать иначе некого',
        )

    try:
        result = await service.create(
            target_url=payload.target_url,
            kind=payload.kind,
            user_id=payload.user_id,
            ttl_hours=payload.ttl_hours,
            idempotency_key=payload.idempotency_key,
        )
    except InvalidTargetUrl as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except CodeGenerationFailed as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    if not result.created:
        response.status_code = status.HTTP_200_OK
    return _to_response(result.link)


@router.get(
    '/{code}',
    response_model=LinkInfoResponse,
    summary='Что стоит за кодом',
    description=(
        'Интроспекция: идентификатор пользователя, срок действия, целевой адрес и счётчик визитов. '
        'Существует затем, чтобы требование «ссылка включает эти три вещи» было проверяемым — '
        'короткий код и есть непрозрачный ключ к ним. Наружу не публикуется.'
    ),
    responses={status.HTTP_404_NOT_FOUND: {'description': 'Кода нет'}},
)
async def get_link(code: str, service: LinkService = Depends(get_link_service)):
    link = await service.get(code)
    if link is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Ссылка не найдена')
    return LinkInfoResponse(
        code=link.code,
        short_url=build_short_url(link.code),
        kind=link.kind,
        user_id=link.user_id,
        target_url=link.target_url,
        expires_at=link.expires_at,
        visit_count=link.visit_count,
        created_at=link.created_at,
        revoked_at=link.revoked_at,
        first_visited_at=link.first_visited_at,
        last_visited_at=link.last_visited_at,
    )


def _to_response(link) -> LinkResponse:
    return LinkResponse(
        code=link.code,
        short_url=build_short_url(link.code),
        kind=link.kind,
        user_id=link.user_id,
        target_url=link.target_url,
        expires_at=link.expires_at,
    )
