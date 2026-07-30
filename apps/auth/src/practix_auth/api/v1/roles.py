import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from practix_auth.api.v1.dependencies import get_role_service, role_required
from practix_auth.models.schemas import RoleCreate, RoleResponse, RoleUpdate
from practix_auth.services.role_service import RoleService

router = APIRouter()


@router.post(
    '/',
    response_model=RoleResponse,
    status_code=status.HTTP_201_CREATED,
    summary='Создать роль',
    description='Создание новой роли в системе (требуются права администратора).',
    responses={
        status.HTTP_403_FORBIDDEN: {'description': 'Недостаточно прав'},
    },
)
async def create_role(
    role_data: RoleCreate, role_service: RoleService = Depends(get_role_service), _=Depends(role_required(['admin']))
):
    return await role_service.create_role(role_data.name, role_data.description)


@router.get(
    '/',
    response_model=list[RoleResponse],
    summary='Список ролей',
    description='Получение списка всех ролей в системе (требуются права администратора).',
    responses={
        status.HTTP_403_FORBIDDEN: {'description': 'Недостаточно прав'},
    },
)
async def get_roles(role_service: RoleService = Depends(get_role_service), _=Depends(role_required(['admin']))):
    return await role_service.get_roles()


@router.post(
    '/assign',
    status_code=status.HTTP_200_OK,
    summary='Назначить роль',
    description='Назначение роли пользователю (требуются права администратора).',
    responses={
        status.HTTP_404_NOT_FOUND: {'description': 'Пользователь или роль не найдены'},
        status.HTTP_403_FORBIDDEN: {'description': 'Недостаточно прав'},
    },
)
async def assign_role(
    user_id: uuid.UUID,
    role_name: str,
    role_service: RoleService = Depends(get_role_service),
    _=Depends(role_required(['admin'])),
):
    try:
        await role_service.assign_role(user_id, role_name)
        return {'msg': 'Роль успешно назначена'}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e


@router.post(
    '/remove',
    status_code=status.HTTP_200_OK,
    summary='Отозвать роль',
    description='Удаление роли у пользователя (требуются права администратора).',
    responses={
        status.HTTP_404_NOT_FOUND: {'description': 'Пользователь или роль не найдены'},
        status.HTTP_403_FORBIDDEN: {'description': 'Недостаточно прав'},
    },
)
async def remove_role(
    user_id: uuid.UUID,
    role_name: str,
    role_service: RoleService = Depends(get_role_service),
    _=Depends(role_required(['admin'])),
):
    try:
        await role_service.remove_role(user_id, role_name)
        return {'msg': 'Роль успешно отозвана'}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e


@router.patch(
    '/{role_id}',
    response_model=RoleResponse,
    summary='Изменить роль',
    description='Обновление названия или описания роли (требуются права администратора).',
    responses={
        status.HTTP_404_NOT_FOUND: {'description': 'Роль не найдена'},
        status.HTTP_403_FORBIDDEN: {'description': 'Недостаточно прав'},
    },
)
async def update_role(
    role_id: uuid.UUID,
    role_data: RoleUpdate,
    role_service: RoleService = Depends(get_role_service),
    _=Depends(role_required(['admin'])),
):
    try:
        return await role_service.update_role(role_id, role_data.name, role_data.description)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e


@router.delete(
    '/{role_id}',
    status_code=status.HTTP_204_NO_CONTENT,
    summary='Удалить роль',
    description='Полное удаление роли из системы (требуются права администратора).',
    responses={
        status.HTTP_404_NOT_FOUND: {'description': 'Роль не найдена'},
        status.HTTP_403_FORBIDDEN: {'description': 'Недостаточно прав'},
    },
)
async def delete_role(
    role_id: uuid.UUID, role_service: RoleService = Depends(get_role_service), _=Depends(role_required(['admin']))
):
    try:
        await role_service.delete_role(role_id)
        return None
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
