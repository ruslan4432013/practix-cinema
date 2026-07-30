"""Определение типа устройства по строке User-Agent.

Тип устройства используется как ключ секционирования (LIST) для таблицы
``login_history``: web / mobile / smart. Всё, что не удалось распознать,
трактуется как ``web`` и в БД попадает в DEFAULT-секцию ``*_other``.
"""

from user_agents import parse

# Допустимые значения ключа секционирования по устройству.
WEB = 'web'
MOBILE = 'mobile'
SMART = 'smart'

# Ключевые слова, указывающие на SmartTV / приставку / консоль.
# Библиотека user-agents не выделяет такие устройства отдельным флагом,
# поэтому дополнительно ищем маркеры в самой строке и в device.family.
_SMART_MARKERS = (
    'smart-tv',
    'smarttv',
    'smart tv',
    'appletv',
    'apple tv',
    'googletv',
    'google tv',
    'android tv',
    'crkey',  # Chromecast
    'hbbtv',
    'netcast',  # LG
    'tizen',  # Samsung
    'web0s',  # LG webOS
    'webos',
    'playstation',
    'xbox',
    'nintendo',
    'roku',
    'aft',  # Amazon Fire TV device ids (AFTM, AFTB, ...)
)


def detect_device_type(user_agent: str) -> str:
    """Возвращает тип устройства: ``web``, ``mobile`` или ``smart``.

    Никогда не бросает исключение: при пустом или нераспознанном
    User-Agent возвращает ``web``.
    """
    ua_raw = (user_agent or '').strip()
    if not ua_raw:
        return WEB

    lowered = ua_raw.lower()

    parsed = parse(ua_raw)
    device_family = (parsed.device.family or '').lower()

    if any(marker in lowered for marker in _SMART_MARKERS) or 'tv' in device_family:
        return SMART

    if parsed.is_mobile or parsed.is_tablet:
        return MOBILE

    return WEB
