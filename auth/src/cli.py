import asyncio

import typer
from async_fastapi_jwt_auth import AuthJWT
from pydantic import BaseModel
from sqlalchemy import select

from core.config import settings
from db.postgres import async_session
from models.entity import User
from models.schemas import UserCreate
from services.auth_service import AuthService
from services.role_service import RoleService

cli = typer.Typer()

SUPERUSER_ROLE_NAME = 'admin'
SUPERUSER_ROLE_DESCRIPTION = 'Администратор системы (суперпользователь)'


class JWTSettings(BaseModel):
    authjwt_secret_key: str = settings.AUTHJWT_SECRET_KEY


@AuthJWT.load_config
def get_config():
    """Загрузка конфигурации для CLI."""
    return JWTSettings()


async def _create_superuser(login: str, email: str, password: str) -> None:
    """Логика создания суперпользователя (идемпотентная)."""
    async with async_session() as db:
        auth_service = AuthService(db, AuthJWT())
        role_service = RoleService(db)

        # 1) Получаем или создаём пользователя
        result = await db.execute(select(User).where((User.login == login) | (User.email == email)))
        user = result.scalar_one_or_none()

        if user is None:
            try:
                user = await auth_service.register(UserCreate(login=login, email=email, password=password))
            except ValueError as exc:
                typer.echo(f'Ошибка регистрации: {exc}')
                raise typer.Exit(code=1) from exc
        else:
            typer.echo(f'Пользователь {login} уже существует — будет повышен до суперпользователя')

        # 2) Создаём роль администратора (если её ещё нет)
        existing_roles = {r.name for r in await role_service.get_roles()}
        if SUPERUSER_ROLE_NAME not in existing_roles:
            await role_service.create_role(SUPERUSER_ROLE_NAME, SUPERUSER_ROLE_DESCRIPTION)

        # 3) Назначаем роль суперпользователя
        await role_service.assign_role(user.id, SUPERUSER_ROLE_NAME)

        typer.echo(f'Суперпользователь {login} успешно создан/обновлён')


@cli.command('createsuperuser')
def create_superuser(
    login: str = typer.Option(..., '--login', '-l', help='Логин суперпользователя'),
    email: str = typer.Option(..., '--email', '-e', help='Email суперпользователя'),
    password: str = typer.Option(..., prompt=True, hide_input=True, confirmation_prompt=True, help='Пароль'),
) -> None:
    """CLI-команда для создания суперпользователя с правами администратора."""
    asyncio.run(_create_superuser(login, email, password))


if __name__ == '__main__':
    cli()
