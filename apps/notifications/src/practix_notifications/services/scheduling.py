"""Арифметика расписаний: когда рассылка должна сработать и сколько раз.

Чистый модуль — ни Django, ни базы. Здесь живёт то, что ломается тише всего:
пересчёт следующего запуска, поведение после простоя планировщика и формат ключа
запуска. Ошибка в любом из трёх не роняет сервис, а просто отправляет письмо
дважды или не отправляет вовсе.

## Всё считается в таймзоне расписания, хранится в UTC

«Каждую пятницу в полдень» — это полдень в часовом поясе, который выбрал
менеджер, а не в UTC. Поэтому cron разворачивается в локальном времени, а
результат конвертируется в UTC. Побочный эффект перехода на летнее время: слот
в «пропавший» час не наступает, а в «повторившийся» — наступает один раз, потому
что ключ запуска строится из ЛОКАЛЬНОГО планового времени и второй проход
натыкается на тот же ключ.

## Что происходит после простоя

Требование задания: «в случае простоя генератора автоматических уведомлений
после его запуска не должны дублироваться старые и новые события». Реализуется
двумя вещами. Первая — политика догона (``catchup``): по умолчанию из всех
пропущенных слотов срабатывает только последний, остальные перескакиваются.
Вторая — уникальный ``run_key``: даже при ``catchup=True`` каждый слот породит
ровно один запуск, потому что второй попадёт в конфликт уникальности.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import croniter

#: Предохранитель на случай «минутного» cron и месячного простоя: 43 200 слотов
#: за раз никому не нужны, а память кончится раньше, чем цикл.
MAX_CATCHUP_SLOTS = 1000


class ScheduleError(ValueError):
    """Расписание описано неверно (кривой cron, неизвестная таймзона)."""


def validate_cron(expression: str) -> None:
    if not croniter.is_valid(expression):
        raise ScheduleError(f'Некорректное cron-выражение: {expression!r}')


def resolve_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    # ZoneInfo кидает разные типы на разных платформах — ловим широко осознанно.
    except Exception as exc:
        raise ScheduleError(f'Неизвестная таймзона: {name!r}') from exc


def deferred_run_at(now: datetime, hours: float) -> datetime:
    """«Отправить через N часов» → абсолютный момент в UTC.

    Отложенная рассылка хранится именно так. Сохрани мы «через 3 часа», после
    перезапуска планировщика было бы не от чего отсчитывать.
    """
    if hours <= 0:
        raise ScheduleError('Отсрочка должна быть положительной')
    return _as_utc(now) + timedelta(hours=hours)


def next_run_at(expression: str, *, after: datetime, timezone: str) -> datetime:
    """Первый слот строго позже ``after`` (оба момента — UTC)."""
    validate_cron(expression)
    tz = resolve_timezone(timezone)
    local_after = _as_utc(after).astimezone(tz)
    nxt = croniter(expression, local_after).get_next(datetime)
    return nxt.astimezone(UTC)


def slots_between(expression: str, *, after: datetime, until: datetime, timezone: str) -> list[datetime]:
    """Все слоты в интервале ``(after, until]`` — в UTC, по возрастанию."""
    validate_cron(expression)
    tz = resolve_timezone(timezone)
    after_utc, until_utc = _as_utc(after), _as_utc(until)
    if until_utc <= after_utc:
        return []

    cursor = croniter(expression, after_utc.astimezone(tz))
    slots: list[datetime] = []
    while len(slots) < MAX_CATCHUP_SLOTS:
        candidate = cursor.get_next(datetime).astimezone(UTC)
        if candidate > until_utc:
            break
        slots.append(candidate)
    return slots


def plan_recurring(
    expression: str,
    *,
    timezone: str,
    due_at: datetime,
    now: datetime,
    catchup: bool,
    ends_at: datetime | None = None,
    max_runs: int | None = None,
    runs_count: int = 0,
) -> tuple[list[datetime], datetime | None]:
    """Что запустить прямо сейчас и когда проснуться в следующий раз.

    :param due_at: слот, из-за которого расписание попало в выборку (его
        ``next_run_at``).
    :returns: ``(слоты к запуску, следующий next_run_at или None)``. ``None``
        означает «расписание отработало своё и больше не проснётся».
    """
    now_utc, due_utc = _as_utc(now), _as_utc(due_at)
    if due_utc > now_utc:
        return [], due_utc

    if catchup:
        # Сам due_at плюс всё, что накопилось после него.
        slots = [due_utc, *slots_between(expression, after=due_utc, until=now_utc, timezone=timezone)]
    else:
        # Только последний пропущенный: пользователю не нужны десять одинаковых
        # писем за десять простоявших пятниц.
        missed = slots_between(expression, after=due_utc, until=now_utc, timezone=timezone)
        slots = [missed[-1] if missed else due_utc]

    if ends_at is not None:
        slots = [slot for slot in slots if slot <= _as_utc(ends_at)]

    if max_runs is not None:
        remaining = max(max_runs - runs_count, 0)
        slots = slots[:remaining]

    following = next_run_at(expression, after=now_utc, timezone=timezone)
    if ends_at is not None and following > _as_utc(ends_at):
        following = None
    if max_runs is not None and runs_count + len(slots) >= max_runs:
        following = None
    return slots, following


def run_key(schedule_id: str, planned_for: datetime, *, timezone: str = 'UTC') -> str:
    """Ключ запуска — единица идемпотентности генератора событий.

    Строится из ЛОКАЛЬНОГО планового времени: при переводе часов назад один и тот
    же час наступает дважды, и ключ по UTC дал бы два разных запуска для того,
    что менеджер видит как одно «в 02:30».

    ФОРМАТ СТАБИЛЕН. Изменение формата задним числом = потеря идемпотентности для
    всех уже сохранённых запусков, поэтому он закреплён юнит-тестом.
    """
    local = _as_utc(planned_for).astimezone(resolve_timezone(timezone))
    return f'{schedule_id}:{local.replace(microsecond=0).isoformat()}'


def manual_run_key(campaign_id: str, at: datetime) -> str:
    """Ключ ручного запуска «Отправить сейчас».

    Секундная гранулярность — сознательная: двойной клик по кнопке в админке в
    пределах секунды не должен превратиться в две рассылки.
    """
    moment = _as_utc(at).replace(microsecond=0)
    return f'{campaign_id}:manual:{moment.isoformat()}'


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ScheduleError('Ожидается datetime с таймзоной: наивное время неотличимо от чужого часового пояса')
    return moment.astimezone(UTC)
