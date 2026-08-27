"""Схемы межсервисного API.

``extra='forbid'`` везде: вызывающий — сервис, и опечатка в имени поля должна
падать на нём кодом 422, а не молча превращаться в ссылку с чужим сроком жизни.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from practix_link_shortener.models.entity import KIND_CONFIRM_EMAIL, KIND_REDIRECT


class LinkCreateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    #: Куда увести. Он же `redirectUrl` из задания. Проверяется по белому списку
    #: хостов при создании — не при переходе: к переходу ссылка уже в письме.
    target_url: str = Field(max_length=2000)
    kind: Literal[KIND_REDIRECT, KIND_CONFIRM_EMAIL] = KIND_REDIRECT
    #: Обязателен для kind='confirm_email' — иначе подтверждать некого.
    user_id: uuid.UUID | None = None
    ttl_hours: int | None = Field(default=None, ge=1)
    #: Ключ идемпотентности вызывающего: повторный вызов с тем же ключом обязан
    #: вернуть ТУ ЖЕ ссылку. Иначе пересобранная пачка писем родила бы человеку
    #: вторую ссылку, а счётчик визитов размазался бы по двум строкам.
    idempotency_key: str | None = Field(default=None, max_length=64)


class LinkResponse(BaseModel):
    """Ответ на создание. ``short_url`` — то, что уедет в письмо."""

    model_config = ConfigDict(from_attributes=True)

    code: str
    short_url: str
    kind: str
    user_id: uuid.UUID | None
    target_url: str
    expires_at: datetime


class LinkInfoResponse(LinkResponse):
    """Интроспекция.

    Существует затем, чтобы требование задания «сокращённая ссылка ДОЛЖНА
    ВКЛЮЧАТЬ идентификатор пользователя, временну́ю метку и redirectUrl» было
    проверяемым, а не только объяснимым: короткий код — непрозрачный ключ,
    который резолвится ровно в эти три поля. Наружу ручка не публикуется.
    """

    visit_count: int
    created_at: datetime
    revoked_at: datetime | None
    first_visited_at: datetime | None
    last_visited_at: datetime | None
