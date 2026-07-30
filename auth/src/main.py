from async_fastapi_jwt_auth import AuthJWT
from async_fastapi_jwt_auth.exceptions import AuthJWTException
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from api.v1 import auth, oauth, roles, users
from core.config import settings
from core.logger import setup_logging
from core.rate_limit import RateLimitMiddleware
from core.request_id import RequestIdMiddleware
from core.tracing import init_tracer_provider, instrument_app
from db.redis import get_redis

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


class JWTSettings(BaseModel):
    authjwt_secret_key: str = settings.AUTHJWT_SECRET_KEY
    authjwt_denylist_enabled: bool = settings.AUTHJWT_DENYLIST_ENABLED
    authjwt_denylist_token_checks: set = settings.AUTHJWT_DENYLIST_TOKEN_CHECKS
    authjwt_access_token_expires: int = settings.ACCESS_TOKEN_EXPIRES
    authjwt_refresh_token_expires: int = settings.REFRESH_TOKEN_EXPIRES


@AuthJWT.load_config
def get_config():
    """Загрузка конфигурации JWT."""
    return JWTSettings()


@AuthJWT.token_in_denylist_loader
async def check_if_token_in_denylist(decrypted_token):
    """Проверка наличия токена в списке отозванных."""
    jti = decrypted_token['jti']
    redis = await get_redis()
    entry = await redis.get(jti)
    return entry is not None


@app.exception_handler(AuthJWTException)
def authjwt_exception_handler(request: Request, exc: AuthJWTException):
    """Обработчик исключений JWT."""
    return JSONResponse(status_code=exc.status_code, content={'detail': exc.message})


app.include_router(auth.router, prefix='/api/v1/auth', tags=['Авторизация'])
app.include_router(roles.router, prefix='/api/v1/roles', tags=['Роли'])
app.include_router(users.router, prefix='/api/v1/users', tags=['Пользователи'])

# OAuth — опциональная фича, монтируется только при OAUTH_ENABLED.
if settings.OAUTH_ENABLED:
    app.include_router(oauth.router, prefix='/api/v1/oauth', tags=['OAuth'])
