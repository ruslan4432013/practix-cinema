import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from practix_auth.models.entity import Role, User


class RoleService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_role(self, name: str, description: str | None = None) -> Role:
        """Создание новой роли."""
        new_role = Role(name=name, description=description)
        self.db.add(new_role)
        await self.db.commit()
        await self.db.refresh(new_role)
        return new_role

    async def get_roles(self) -> list[Role]:
        """Получение всех доступных ролей."""
        result = await self.db.execute(select(Role))
        return result.scalars().all()

    async def assign_role(self, user_id: uuid.UUID, role_name: str):
        """Назначение роли пользователю."""
        result = await self.db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            raise ValueError('Пользователь не найден')

        result = await self.db.execute(select(Role).where(Role.name == role_name))
        role = result.scalar_one_or_none()
        if not role:
            raise ValueError('Роль не найдена')

        if role not in user.roles:
            user.roles.append(role)
            await self.db.commit()

    async def remove_role(self, user_id: uuid.UUID, role_name: str):
        """Отзыв роли у пользователя."""
        result = await self.db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            raise ValueError('Пользователь не найден')

        result = await self.db.execute(select(Role).where(Role.name == role_name))
        role = result.scalar_one_or_none()
        if not role:
            raise ValueError('Роль не найдена')

        if role in user.roles:
            user.roles.remove(role)
            await self.db.commit()

    async def update_role(self, role_id: uuid.UUID, name: str | None = None, description: str | None = None) -> Role:
        """Обновление информации о роли."""
        result = await self.db.execute(select(Role).where(Role.id == role_id))
        role = result.scalar_one_or_none()
        if not role:
            raise ValueError('Роль не найдена')

        if name:
            role.name = name
        if description is not None:
            role.description = description

        await self.db.commit()
        await self.db.refresh(role)
        return role

    async def delete_role(self, role_id: uuid.UUID):
        """Удаление роли."""
        result = await self.db.execute(select(Role).where(Role.id == role_id))
        role = result.scalar_one_or_none()
        if not role:
            raise ValueError('Роль не найдена')

        await self.db.delete(role)
        await self.db.commit()
