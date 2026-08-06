"""Проверка окружения и небезопасных значений по умолчанию.

## Зачем это в библиотеке

Три сервиса (``ugc-api``, ``notifications``, ``link-shortener``) объявляли у себя
одинаковый валидатор: «имя окружения из белого списка; в ``prod`` не подниматься,
если секреты остались в значениях по умолчанию». Отличались они только префиксом
ключа в сообщении об ошибке.

Дублировалась не формулировка, а ПРАВИЛО — и расходится оно молча. Стоило бы
кому-то добавить четвёртое окружение или ослабить проверку в одном сервисе, и
разница обнаружилась бы на продакшене одного из трёх.

## Почему настройки не импортируются

То же правило, что у ``practix_core.logging``, ``tracing`` и ``sentry``: только
stdlib, никаких настроек и никаких побочных эффектов на импорте. Отличия
сервисов выражены аргументами.
"""

from collections.abc import Iterable

#: Окружения, известные всем сервисам репозитория. Список закрытый намеренно:
#: опечатка вроде ``prodction`` иначе тихо включила бы послабления разработки.
KNOWN_ENVIRONMENTS = frozenset({'dev', 'test', 'prod'})

#: Окружение, в котором небезопасные значения по умолчанию запрещены.
HARDENED_ENVIRONMENT = 'prod'


def validate_environment(
    value: str,
    *,
    key: str,
    insecure_defaults: Iterable[str] = (),
    known: frozenset[str] = KNOWN_ENVIRONMENTS,
) -> str:
    """Проверить имя окружения и запретить старт в проде с дефолтными секретами.

    Возвращает нормализованное имя окружения. Бросает ``ValueError`` — его
    ``pydantic`` превращает в понятную ошибку валидации настроек.

    :param key: имя переменной окружения (``UGC_API_ENV``, ``SHORTENER_ENV``…).
        Нужно только для текста ошибки: человек, читающий её в логе упавшего
        контейнера, должен узнать, что именно править.
    :param insecure_defaults: описания секретов, оставшихся в значении по
        умолчанию. Опасность таких значений в том, что они РАБОТАЮТ: сервис
        поднимается, ничего не ломается, и подмену забывают.
    """
    normalized = value.strip().lower()
    if normalized not in known:
        raise ValueError(f'{key} must be one of {sorted(known)}, got {value!r}')

    problems = list(insecure_defaults)
    if normalized == HARDENED_ENVIRONMENT and problems:
        raise ValueError('Insecure default values must be overridden in production: ' + '; '.join(problems))
    return normalized
