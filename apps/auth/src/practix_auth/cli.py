"""Обслуживающие команды Auth.

    python -m practix_auth.cli createsuperuser --login … --email … --password …
    python -m practix_auth.cli create-service-account --login … --email … --password … --role …

Раньше команда была одна, Typer схлопывал приложение, и подкоманда НЕ писалась.
С появлением второй схлопывание отключено пустым ``callback`` (как в
``practix_ugc_api.cli``), поэтому подкоманда обязательна. Если правите вызовы —
их четыре: ``infra/compose/docker-compose.test.yml``, ``docs/quickstart.md``,
``CLAUDE.md`` и этот файл.
"""

import asyncio

import typer
from async_fastapi_jwt_auth import AuthJWT
from sqlalchemy import select

from practix_auth.core.config import settings
from practix_auth.core.roles import EMAIL_CONFIRMER_ROLE
from practix_auth.db.postgres import async_session
from practix_auth.models.entity import User
from practix_auth.models.schemas import UserCreate
from practix_auth.services.auth_service import AuthService
from practix_auth.services.role_service import RoleService
from practix_core.jwt import install_config_loader, make_jwt_settings

cli = typer.Typer(help='Обслуживающие команды сервиса Auth.')


@cli.callback()
def _root() -> None:
    """Пустой callback: не даёт Typer схлопнуть приложение до одной команды."""


SUPERUSER_ROLE_NAME = 'admin'
SUPERUSER_ROLE_DESCRIPTION = 'Администратор системы (суперпользователь)'

#: Человекочитаемые описания служебных ролей. Роль создаётся командой, если её
#: ещё нет, и без описания в списке ролей осталось бы голое имя.
SERVICE_ROLE_DESCRIPTIONS = {
    EMAIL_CONFIRMER_ROLE: 'Служебная роль: подтверждение email по короткой ссылке',
}

# CLI не выпускает и не проверяет токены — ему нужен лишь корректно
# сконфигурированный AuthJWT, чтобы импортировался AuthService. Поэтому здесь
# только секрет, без денилиста и без сроков жизни: это была четвёртая (самая
# урезанная) копия JWTSettings в репозитории.
install_config_loader(lambda: make_jwt_settings(authjwt_secret_key=settings.AUTHJWT_SECRET_KEY))


async def _ensure_user_with_role(login: str, email: str, password: str, role: str, role_description: str) -> None:
    """Завести (или найти) пользователя и выдать ему роль. Идемпотентно.

    Общая для обеих команд: суперпользователь и служебная учётка отличаются
    только именем роли. Развести их на две копии значило бы получить два места,
    где чинить «роль не создалась, если её ещё не было».
    """
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
            typer.echo(f'Пользователь {login} уже существует — ему будет выдана роль {role}')

        # 2) Создаём роль (если её ещё нет)
        existing_roles = {r.name for r in await role_service.get_roles()}
        if role not in existing_roles:
            await role_service.create_role(role, role_description)

        # 3) Назначаем роль
        await role_service.assign_role(user.id, role)

        typer.echo(f'Пользователь {login} с ролью {role} успешно создан/обновлён')


@cli.command('createsuperuser')
def create_superuser(
    login: str = typer.Option(..., '--login', '-l', help='Логин суперпользователя'),
    email: str = typer.Option(..., '--email', '-e', help='Email суперпользователя'),
    password: str = typer.Option(..., prompt=True, hide_input=True, confirmation_prompt=True, help='Пароль'),
) -> None:
    """CLI-команда для создания суперпользователя с правами администратора."""
    asyncio.run(_ensure_user_with_role(login, email, password, SUPERUSER_ROLE_NAME, SUPERUSER_ROLE_DESCRIPTION))


@cli.command('create-service-account')
def create_service_account(
    login: str = typer.Option(..., '--login', '-l', help='Логин служебной учётки'),
    email: str = typer.Option(..., '--email', '-e', help='Email служебной учётки'),
    password: str = typer.Option(..., prompt=True, hide_input=True, confirmation_prompt=True, help='Пароль'),
    role: str = typer.Option(..., '--role', '-r', help='Роль, которую выдать (например, email-confirmer)'),
) -> None:
    """Учётка для межсервисного вызова: обычный пользователь с ОДНОЙ узкой ролью.

    Именно пользователь, а не самовыписанный токен: токен, который сервис
    подписывает себе сам, обходит и роли, и денилист — то есть отозвать доступ
    такому сервису нечем. И именно узкая роль, а не ``admin``: компрометация
    одного сервиса не должна открывать всю административную поверхность.
    """
    asyncio.run(_ensure_user_with_role(login, email, password, role, SERVICE_ROLE_DESCRIPTIONS.get(role, role)))


if __name__ == '__main__':
    cli()
