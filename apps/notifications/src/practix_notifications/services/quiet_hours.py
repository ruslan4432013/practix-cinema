"""Тихие часы: не писать пользователю ночью.

«Самая опасная ошибка при отправке писем — отправить его ночью, когда клиент
спит» (теория, «Как испортить жизнь клиенту»). Ошибка эта не техническая:
рассылка уходит успешно, метрики зелёные, а утром приходят жалобы.

Окно применяется в таймзоне ПОЛУЧАТЕЛЯ. Тихие часы по времени сервера — это
тихие часы для одного часового пояса и ничьи для остальных.

Чистый модуль: ни Django, ни настроек. Значения передаёт вызывающая сторона.
"""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo


class QuietHoursError(ValueError):
    pass


def parse_time(raw: str) -> time:
    """``'22:00'`` → ``time(22, 0)``."""
    try:
        hours, minutes = raw.strip().split(':')
        return time(int(hours), int(minutes))
    except (ValueError, AttributeError) as exc:
        raise QuietHoursError(f'Ожидается время в формате ЧЧ:ММ, получено {raw!r}') from exc


def is_quiet(moment: datetime, *, timezone: str, start: time, end: time) -> bool:
    """Попадает ли момент в тихие часы получателя.

    Окно почти всегда переходит через полночь (22:00 → 09:00), поэтому проверка
    двусторонняя, а не одним диапазоном.
    """
    if start == end:
        return False
    local = _local(moment, timezone).time()
    if start < end:
        return start <= local < end
    return local >= start or local < end


def next_allowed_at(moment: datetime, *, timezone: str, start: time, end: time) -> datetime:
    """Ближайший момент (UTC), когда писать снова можно.

    Возвращает сам ``moment``, если тихих часов сейчас нет, — вызывающему коду не
    нужно проверять дважды.
    """
    if not is_quiet(moment, timezone=timezone, start=start, end=end):
        return moment.astimezone(UTC)

    local = _local(moment, timezone)
    candidate = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def _local(moment: datetime, timezone: str) -> datetime:
    if moment.tzinfo is None:
        raise QuietHoursError('Ожидается datetime с таймзоной')
    try:
        return moment.astimezone(ZoneInfo(timezone))
    # ZoneInfo кидает разные типы на разных платформах — ловим широко осознанно.
    except Exception as exc:
        raise QuietHoursError(f'Неизвестная таймзона: {timezone!r}') from exc
