from typing import Protocol
from uuid import UUID


class EntityWithDatabase(Protocol):
    __tablename__: str
    id: UUID

    def __init__(self, **kwargs: any) -> None: ...
