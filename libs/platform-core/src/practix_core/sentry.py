"""Инициализация Sentry-совместимого сбора ошибок (Sentry / GlitchTip).

Отвечает на вопрос, который не закрывает ни один из уже подключённых
инструментов: Jaeger показывает, ГДЕ ушло время, Prometheus — СКОЛЬКО и КАК
ЧАСТО, и только сборщик ошибок показывает, ЧТО именно упало и с каким стеком.
До этого модуля необработанное исключение было видно лишь как строка
``logger.exception`` в stdout контейнера, а в фоновых ETL — вообще проглатывалось
блоками ``except Exception``.

Приёмником может быть и sentry.io, и self-hosted Sentry, и GlitchTip: протокол
приёма событий у них общий, и переезд между ними — это замена ``SENTRY_DSN`` и
ни одной строки кода.

## Главный инвариант: Sentry ловит ОШИБКИ, трассировку ведёт Jaeger

``sentry_sdk`` умеет собирать и performance-трейсы, но его OTel-интеграция
устанавливает СВОЙ глобальный ``TracerProvider``
(``_setup_sentry_tracing()`` → ``trace.set_tracer_provider(...)``), а в
``practix_core.tracing.init_tracer_provider`` провайдер уже наш, с OTLP-экспортом
в Jaeger. Две системы за один глобальный провайдер драться не должны: победит та,
что инициализировалась второй, и трассировка молча уедет не туда.

Поэтому ``traces_sample_rate`` НЕ ПЕРЕДАЁТСЯ ВООБЩЕ. Именно вообще, а не нулём:
по докстрингу SDK ``0`` означает «не начинать новые трейсы, но продолжать
входящие», то есть трассировка остаётся включённой, а отсутствие параметра
выключает её целиком. ``OpenTelemetryIntegration`` не входит в
``_DEFAULT_INTEGRATIONS`` и сама не подключается — достаточно её не просить.
Инвариант закреплён тестом
``tests/test_sentry.py::test_init_does_not_replace_global_tracer_provider``, а не
только этим комментарием.

Связь между системами — тегами: в каждое событие проставляются ``request_id`` и
``trace_id`` активного OTel-спана. Из ошибки в интерфейсе сборщика ищем трейс в
Jaeger по ``trace_id``, а строки лога — по ``request_id``.

## Почему здесь нет импорта настроек

То же правило, что у ``practix_core.logging`` и ``practix_core.tracing``:
импортируются только stdlib, ``sentry_sdk`` и ``practix_core.context``; никаких
настроек и никаких побочных эффектов на импорте. Отличия сервисов выражены
АРГУМЕНТАМИ, а не форком модуля.

``opentelemetry`` импортируется лениво и под ``try/except ImportError`` — по
образцу ``tracing.instrument_aiokafka``. У ``etl-elasticsearch`` зависимость
``practix-core`` объявлена без extras, OpenTelemetry в его образе нет, и сбор
ошибок обязан работать и там, потеряв лишь тег ``trace_id``.
"""

import logging
from collections.abc import Callable, Mapping, Sequence

import sentry_sdk
from sentry_sdk.integrations import Integration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.scrubber import DEFAULT_DENYLIST, EventScrubber

# Публичный псевдоним SDK для типов события и подсказки. Именно его ждёт
# ``add_event_processor``; собственная аннотация ``dict[str, Any]`` не подходит
# по сигнатуре, хотя во время выполнения это тот же словарь.
from sentry_sdk.types import Event, Hint

from practix_core.context import get_request_id

logger = logging.getLogger(__name__)

_INITIALIZED = False

# Ключи, вырезаемые из событий ДОПОЛНИТЕЛЬНО к штатному DEFAULT_DENYLIST
# (password, token, secret, authorization, cookies и т.д.).
#
# Список не косметический: `include_local_variables` по умолчанию True, то есть в
# кадрах стека едут локальные переменные. При падении внутри логина Auth это
# пароль открытым текстом, при падении в коллекторе — соль хеширования IP,
# по которой хеши перестают быть анонимными.
EXTRA_DENYLIST: tuple[str, ...] = (
    'authjwt_secret_key',
    'access_token',
    'refresh_token',
    'ch_password',
    'ugc_ip_hash_salt',
    'yandex_client_secret',
)


def reset_for_testing() -> None:
    """Сбрасывает флаг идемпотентности. Только для тестов."""
    global _INITIALIZED
    _INITIALIZED = False


def otel_trace_id() -> str | None:
    """``trace_id`` активного OTel-спана в шестнадцатеричном виде или ``None``.

    ``None`` возвращается в трёх штатных случаях: OpenTelemetry не установлен
    (образ ``etl-elasticsearch``), провайдер не сконфигурирован
    (``OTEL_ENABLED=False``) или активного спана нет (фоновая задача вне
    запроса). Ни один из них не является ошибкой.
    """
    try:
        from opentelemetry import trace
    except ImportError:
        return None

    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return format(span_context.trace_id, '032x')


def build_event_processor(
    *,
    static_tags: Mapping[str, str] | None = None,
) -> Callable[[Event, Hint], Event | None]:
    """Собирает processor, дополняющий каждое событие тегами связи с логами и трейсами.

    Вынесен наружу и не замкнут на клиента, чтобы его можно было проверить
    тестом без живого DSN и без сетевого транспорта.
    """

    def processor(event: Event, _hint: Hint) -> Event | None:
        tags = event.setdefault('tags', {})
        if static_tags:
            tags.update(static_tags)

        request_id = get_request_id()
        if request_id:
            tags['request_id'] = request_id

        trace_id = otel_trace_id()
        if trace_id:
            tags['trace_id'] = trace_id

        return event

    return processor


def init_sentry(
    *,
    enabled: bool,
    dsn: str,
    service_name: str,
    environment: str,
    release: str | None = None,
    sample_rate: float = 1.0,
    event_level: int = logging.ERROR,
    breadcrumb_level: int = logging.INFO,
    send_default_pii: bool = False,
    include_local_variables: bool = True,
    extra_denylist: Sequence[str] = EXTRA_DENYLIST,
    integrations: Sequence[Integration] = (),
) -> None:
    """Идемпотентная настройка сбора ошибок.

    :param enabled: общий выключатель. Выключено по умолчанию во всех сервисах:
        без поднятого приёмника SDK молотил бы в пустоту.
    :param service_name: попадает в ``server_name`` и в тег ``service``. Сервисы
        передают сюда ``OTEL_SERVICE_NAME`` — в репозитории это каноническое имя
        сервиса (``PROJECT_NAME`` общий на весь стенд и для этого не годится).
    :param sample_rate: доля отправляемых событий ОБ ОШИБКАХ. Не путать с
        ``traces_sample_rate``, который здесь не используется намеренно —
        см. докстринг модуля.
    :param event_level: уровень лога, с которого запись становится событием.
        ``ERROR`` превращает в события те ``logger.exception`` из проглоченных
        ``except``-блоков ETL, которые иначе видны только в stdout.
    :param breadcrumb_level: уровень, с которого записи копятся «хлебными
        крошками» и прикладываются к следующему событию.
    :param integrations: интеграции сверх включаемых автоматически. FastAPI,
        Starlette, Django, SQLAlchemy, asyncpg и Redis SDK находит сам по факту
        импортируемости пакета, поэтому обычно передавать сюда нечего.
    """
    global _INITIALIZED
    if _INITIALIZED or not enabled:
        return

    if not dsn:
        # Тихий no-op здесь — классическая ловушка «Sentry включён, а событий
        # нет»: разбирательство упирается в то, что DSN просто не подставился.
        logger.warning('Sentry is enabled but SENTRY_DSN is empty — error reporting is off')
        return

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=release or None,
        server_name=service_name,
        sample_rate=sample_rate,
        # traces_sample_rate ОТСУТСТВУЕТ намеренно — см. докстринг модуля.
        send_default_pii=send_default_pii,
        include_local_variables=include_local_variables,
        event_scrubber=EventScrubber(denylist=[*DEFAULT_DENYLIST, *extra_denylist]),
        integrations=[
            # LoggingIntegration входит в набор по умолчанию, но задаётся явно:
            # уровни — это и есть настраиваемая часть, а полагаться на дефолт
            # SDK в вопросе «что считать ошибкой» не стоит.
            LoggingIntegration(level=breadcrumb_level, event_level=event_level),
            *integrations,
        ],
        # Отправка идёт фоновым потоком; при завершении процесса даём ему
        # секунды, а не бесконечность, чтобы не задерживать остановку сервиса.
        shutdown_timeout=2,
    )

    sentry_sdk.get_global_scope().add_event_processor(build_event_processor(static_tags={'service': service_name}))

    _INITIALIZED = True
    logger.info(
        'Sentry error reporting initialized for %s',
        service_name,
        extra={'service': service_name, 'environment': environment},
    )
