"""Обвязка ``async_fastapi_jwt_auth``: конфигурация, денилист, обработчик ошибок.

Дублировалось в трёх приложениях: ``JWTSettings`` — 4 копии (включая заглушку в
``auth/src/cli.py``), ``@AuthJWT.load_config`` — 4 копии с побайтово одинаковым
телом, ``token_in_denylist_loader`` — 3 копии, обработчик ``AuthJWTException`` — 3
копии с побайтово одинаковым телом.

## Политика при недоступности Redis — ПАРАМЕТР, а не общий дефолт

Здесь пришлось отойти от первоначального плана, который предлагал распространить
на все сервисы вариант коллектора («ошибка Redis -> токен считается действующим»).
Для коллектора это верно и обосновано: терять событие аналитики из-за проблем с
кэшем не стоит, а риск ограничен — по отозванному токену можно лишь отправить
событие от своего же имени.

Для **Auth** тот же выбор был бы регрессией безопасности. Auth — это сервис,
который ВЕДЁТ денилист; «fail-open» там означает, что при любом сбое Redis
принимается токен вышедшего из системы пользователя, то есть logout перестаёт
держать. Это разные требования, а не разные стили, поэтому политика задаётся
явно:

* ``on_error='raise'`` — исключение уходит наружу. Текущее поведение
  ``rest``/``auth``; оставлено по умолчанию, чтобы извлечение не меняло поведение
  само по себе.
* ``on_error='allow'`` — токен считается действующим. Выбор коллектора.
* ``on_error='deny'`` — токен считается отозванным. Максимально строгий вариант;
  сейчас не используется, но именно его стоит включать там, где отказ Redis не
  должен открывать доступ.

Смена ``rest`` на ``allow`` обсуждаема отдельно (у него заявлена изящная
деградация при недоступности Auth), но это продуктовое решение, а не побочный
эффект рефакторинга.
"""

import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from async_fastapi_jwt_auth import AuthJWT
from async_fastapi_jwt_auth.exceptions import AuthJWTException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, create_model

logger = logging.getLogger(__name__)

OnDenylistError = Literal['raise', 'allow', 'deny']

# Поля, которые понимает async_fastapi_jwt_auth. Модель настроек собирается
# динамически ровно из переданных значений: лишнее поле со значением None
# перезаписало бы дефолт библиотеки (например, срок жизни токена).
_FIELD_TYPES: dict[str, Any] = {
    'authjwt_secret_key': str,
    'authjwt_denylist_enabled': bool,
    'authjwt_denylist_token_checks': set,
    'authjwt_access_token_expires': int,
    'authjwt_refresh_token_expires': int,
}


def make_jwt_settings(**values: Any) -> BaseModel:
    """Собирает модель настроек JWT из переданных полей.

    Передавать следует только те поля, которые сервис действительно задаёт:
    ``rest`` и коллектор не управляют сроками жизни токенов (они их не выпускают),
    и добавление этих полей со значением ``None`` сломало бы дефолты библиотеки.
    """
    unknown = set(values) - set(_FIELD_TYPES)
    if unknown:
        raise ValueError(f'Неизвестные поля настроек JWT: {sorted(unknown)}')
    # dict[str, Any], а не выведенный dict[str, tuple[type, EllipsisType]]:
    # перегрузки create_model объявлены через **field_definitions, и точный тип
    # значения mypy сопоставить с ними не может.
    fields: dict[str, Any] = {name: (_FIELD_TYPES[name], ...) for name in values}
    model = create_model('JWTSettings', **fields)
    return model(**values)


def install_config_loader(factory: Callable[[], BaseModel]) -> None:
    """Регистрирует ``@AuthJWT.load_config``. Тело было одинаковым во всех копиях."""
    AuthJWT.load_config(factory)


def install_denylist_loader(
    redis_provider: Callable[[], Any | None | Awaitable[Any | None]],
    *,
    on_error: OnDenylistError = 'raise',
) -> None:
    """Регистрирует проверку отозванных токенов.

    :param redis_provider: возвращает клиент Redis. Может быть синхронной или
        асинхронной функцией и может вернуть ``None`` (клиент ещё не создан).
        Источник у сервисов разный: коллектор обязан читать денилист из Redis
        Auth-сервиса, а не из своего — в его базе этих ключей просто нет, и одна
        общая функция искала бы их не там.
    """

    @AuthJWT.token_in_denylist_loader
    async def check_if_token_in_denylist(decrypted_token) -> bool:
        jti = decrypted_token['jti']
        try:
            redis = redis_provider()
            if inspect.isawaitable(redis):
                redis = await redis
            if redis is None:
                # Клиента нет — считаем так же, как при ошибке.
                if on_error == 'raise':
                    raise RuntimeError('Redis client for the JWT denylist is not initialised')
                return on_error == 'deny'
            return await redis.get(jti) is not None
        except Exception as exc:
            if on_error == 'raise':
                raise
            logger.warning('Denylist check failed, Redis unavailable: %s', exc)
            return on_error == 'deny'

    return None


def install_exception_handler(app) -> None:
    """Регистрирует обработчик ``AuthJWTException``. Тело было одинаковым в трёх копиях."""

    @app.exception_handler(AuthJWTException)
    def authjwt_exception_handler(_request, exc: AuthJWTException):
        return JSONResponse(status_code=exc.status_code, content={'detail': exc.message})

    return None
