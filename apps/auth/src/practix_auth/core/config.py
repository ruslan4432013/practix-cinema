from pydantic_settings import BaseSettings, SettingsConfigDict

from practix_core.net import Network, parse_networks


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    PROJECT_NAME: str

    REDIS_HOST: str
    REDIS_PORT: int

    # --- Rate limiting (защита от DDoS / брутфорса) ---
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_TIMES: int = 20  # запросов в окне на один клиентский IP
    RATE_LIMIT_SECONDS: int = 60  # длина окна в секундах
    # Сети, из которых принимаются X-Real-IP / X-Forwarded-For. По умолчанию —
    # диапазоны Docker и приватные сети RFC1918, то есть Nginx из compose.
    # Пустая строка отключает проверку (нужно тестам, где приложение поднято
    # in-process и адреса соединения нет). Значение совпадает с UGC_TRUSTED_PROXIES
    # коллектора: оба сервиса стоят за одним и тем же Nginx.
    AUTH_TRUSTED_PROXIES: str = '172.16.0.0/12,192.168.0.0/16,10.0.0.0/8,127.0.0.0/8'

    AUTH_POSTGRES_DB: str
    AUTH_POSTGRES_USER: str
    AUTH_POSTGRES_PASSWORD: str
    AUTH_POSTGRES_HOST: str
    AUTH_POSTGRES_PORT: int

    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int
    REFRESH_TOKEN_EXPIRE_DAYS: int

    BASE_DIR: str

    AUTHJWT_SECRET_KEY: str
    AUTHJWT_DENYLIST_ENABLED: bool
    AUTHJWT_DENYLIST_TOKEN_CHECKS: set
    ACCESS_TOKEN_EXPIRES: int
    REFRESH_TOKEN_EXPIRES: int
    DEFAULT_ROLE_NAME: str
    SUPERUSER_ROLES: set[str]

    # --- OAuth (social login, consumer side) ---
    # OAuth is optional: when disabled the router is not mounted and the
    # service behaves exactly as before. Blank credentials never break startup.
    # To add a new provider: add its <PROVIDER>_* settings here and register a
    # BaseOAuthProvider subclass in services/oauth_providers.py — the router and
    # service are provider-agnostic (provider name comes from the URL).
    OAUTH_ENABLED: bool = False
    YANDEX_CLIENT_ID: str = ''
    YANDEX_CLIENT_SECRET: str = ''
    YANDEX_REDIRECT_URI: str = 'http://localhost/api/v1/oauth/yandex/callback'
    YANDEX_AUTHORIZE_URL: str = 'https://oauth.yandex.ru/authorize'
    YANDEX_TOKEN_URL: str = 'https://oauth.yandex.ru/token'
    YANDEX_USERINFO_URL: str = 'https://login.yandex.ru/info'
    OAUTH_STATE_TTL: int = 300

    # --- Distributed tracing (OpenTelemetry / Jaeger) ---
    OTEL_ENABLED: bool = True
    OTEL_SERVICE_NAME: str = 'auth-service'
    OTEL_EXPORTER_OTLP_ENDPOINT: str = 'http://jaeger:4318'

    @property
    def trusted_proxy_networks(self) -> list[Network]:
        """Сети, из которых принимаются заголовки с адресом клиента.

        Разбор и обоснование строгости — в ``practix_core.net.parse_networks``.
        """
        return parse_networks(self.AUTH_TRUSTED_PROXIES)


settings = Settings()
