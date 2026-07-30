"""Схема авторизации Bearer в документе OpenAPI.

FastAPI не описывает `Authorization: Bearer <token>` сам, если токен не приходит
через его собственные `security`-зависимости, — а он и не приходит: JWT
проверяется обвязкой ``practix_core.jwt``. Без этой правки Swagger UI показывает
ручки, но не даёт послать токен, и «попробовать» защищённый метод из браузера
нельзя.

Тело функции было побайтово одинаковым в двух сервисах — это ровно тот случай,
который в ``docs/monorepo.md`` отнесён к «обязательному бойлерплейту»: правки
здесь не бывают продиктованы требованиями конкретного сервиса.
"""

from fastapi.openapi.utils import get_openapi

BEARER_SCHEME = {'type': 'http', 'scheme': 'bearer', 'bearerFormat': 'JWT'}


def install_bearer_security(app) -> None:
    """Подменяет ``app.openapi`` так, чтобы документ содержал схему Bearer.

    Документ кешируется в ``app.openapi_schema`` — как это делает и сам FastAPI:
    собирать его заново на каждый запрос к ``/openapi.json`` незачем.
    """

    def custom_openapi() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        schema.setdefault('components', {})['securitySchemes'] = {'Bearer': dict(BEARER_SCHEME)}
        app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = custom_openapi
