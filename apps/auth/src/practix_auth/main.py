from fastapi import FastAPI

from practix_auth.api.v1 import auth, oauth, roles, users
from practix_auth.core.config import settings
from practix_auth.core.logger import setup_logging
from practix_auth.core.rate_limit import RateLimitMiddleware
from practix_auth.core.request_id import RequestIdMiddleware
from practix_auth.core.tracing import init_tracer_provider, instrument_app
from practix_auth.db.redis import get_redis
from practix_core.jwt import (
    install_config_loader,
    install_denylist_loader,
    install_exception_handler,
    make_jwt_settings,
)

# Сервис запускается через `uvicorn src.main:app`, то есть без --log-config.
# Настраиваем логирование при импорте приложения: dictConfig выполняется после
# собственной инициализации uvicorn и перекрывает её (как в коллекторе).
setup_logging()

app = FastAPI(
    title='Сервис авторизации',
    description='API для управления пользователями, ролями и сессиями.',
    docs_url='/api/openapi',
    openapi_url='/api/openapi.json',
)

# Трассировка: сначала наш middleware (внутренний), затем инструментация OTel
# (внешняя) — чтобы серверный span уже существовал при простановке тега.
init_tracer_provider()
app.add_middleware(RequestIdMiddleware)
# Rate limit проверяется раньше валидации X-Request-Id (отсекает флуд раньше),
# но остаётся внутри OTel-инструментации.
app.add_middleware(RateLimitMiddleware)
instrument_app(app)


# Обвязка JWT переехала в practix_core.jwt. Политика при отказе Redis — 'raise'
# (как было). fail-open здесь был бы регрессией безопасности: Auth ВЕДЁТ
# денилист, и принять токен вышедшего из системы пользователя при сбое Redis
# означает, что logout перестаёт держать.
install_config_loader(
    lambda: make_jwt_settings(
        authjwt_secret_key=settings.AUTHJWT_SECRET_KEY,
        authjwt_denylist_enabled=settings.AUTHJWT_DENYLIST_ENABLED,
        authjwt_denylist_token_checks=settings.AUTHJWT_DENYLIST_TOKEN_CHECKS,
        authjwt_access_token_expires=settings.ACCESS_TOKEN_EXPIRES,
        authjwt_refresh_token_expires=settings.REFRESH_TOKEN_EXPIRES,
    )
)
install_denylist_loader(get_redis)
install_exception_handler(app)


app.include_router(auth.router, prefix='/api/v1/auth', tags=['Авторизация'])
app.include_router(roles.router, prefix='/api/v1/roles', tags=['Роли'])
app.include_router(users.router, prefix='/api/v1/users', tags=['Пользователи'])

# OAuth — опциональная фича, монтируется только при OAUTH_ENABLED.
if settings.OAUTH_ENABLED:
    app.include_router(oauth.router, prefix='/api/v1/oauth', tags=['OAuth'])
