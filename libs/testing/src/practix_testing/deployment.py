"""Чтение развёрнутой конфигурации из файлов, которые её и задают.

ЗАЧЕМ ЧИТАТЬ ФАЙЛЫ, А НЕ ПОВТОРЯТЬ ЧИСЛА КОНСТАНТАМИ. Величины, из которых
складывается бюджет соединений к базе, лежат в трёх файлах трёх разных
форматов, и любая из них меняется в одиночку без единого предупреждения:
``--workers`` в Dockerfile, ``max_connections`` в compose, значения пулов в
``.env.example``. Константа в тесте описывала бы прошлое и продолжала бы
проходить после поломки. Здесь тест ломается ровно тогда, когда арифметика
перестаёт сходиться, — и говорит, какое из чисел поехало.

ЗАЧЕМ ЭТО БИБЛИОТЕКА, А НЕ КОПИЯ В КАЖДОМ НАБОРЕ. Проверка нужна трём сервисам
(`recommendations-api`, `ugc-api`, `link-shortener`), а разбор Dockerfile и
compose — это два десятка строк регулярных выражений, одинаковых до символа.
Третья копия перевела бы `npm run dup` через порог, и это был бы честный
сигнал: дублируется не формулировка, а СПОСОБ читать чужие файлы, который
поедет молча, стоит кому-то переписать CMD или переформатировать compose. В
наборах остаются только числа и утверждения о них — то, что у каждого сервиса
своё.
"""

import re
from functools import cache
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings

# Резерв суперпользователя PostgreSQL (`superuser_reserved_connections`, по
# умолчанию 3): эти слоты недоступны обычной роли. Считать их своими — способ
# обнаружить их отсутствие ровно в момент аварии, когда подключиться уже нечем.
SUPERUSER_RESERVED = 3


@cache
def repo_root() -> Path:
    """Корень монорепозитория — по файлу, который есть только в нём.

    Считать уровни вложенности (``parents[4]``) нельзя: путь до набора тестов
    у каждого сервиса свой, и переезд каталога сломал бы поиск молча, дав
    ``FileNotFoundError`` вместо внятного отказа.
    """
    marker = Path('infra') / 'docker' / 'python-service.Dockerfile'
    for candidate in Path(__file__).resolve().parents:
        if (candidate / marker).exists():
            return candidate
    raise RuntimeError(f'не найден корень репозитория: нет {marker} ни у одного из родителей')


def dockerfile_target(name: str) -> str:
    """Тело таргета: от его ``FROM`` до следующего."""
    dockerfile = repo_root() / 'infra' / 'docker' / 'python-service.Dockerfile'
    body = dockerfile.read_text(encoding='utf-8').split(f'FROM runtime AS {name}\n', 1)[1]
    return body.split('\nFROM ', 1)[0]


def compose_service(name: str) -> str:
    """Тело сервиса compose: от его ключа до следующего на том же уровне."""
    lines = (repo_root() / 'infra' / 'compose' / 'docker-compose.yml').read_text(encoding='utf-8').splitlines()
    start = lines.index(f'  {name}:') + 1
    body: list[str] = []
    for line in lines[start:]:
        if re.match(r'^ {2}\S', line):
            break
        body.append(line)
    return '\n'.join(body)


def env_example_int(key: str) -> int:
    """Значение целочисленного ключа из корневого ``.env.example``.

    Проверять надо именно шаблон: в образ entrypoint'ом копируется он, поэтому
    эффективные значения на стенде — его, а не питоновские умолчания.
    """
    template = (repo_root() / '.env.example').read_text(encoding='utf-8')
    match = re.search(rf'^{key}=(\d+)$', template, re.MULTILINE)
    assert match, f'{key} пропал из .env.example — образ поедет на умолчаниях кода'
    return int(match.group(1))


def uvicorn_workers(target: str) -> int:
    """Сколько воркеров uvicorn поднимает CMD таргета."""
    match = re.search(r'--workers",\s*"(\d+)"', dockerfile_target(target))
    assert match, f'у таргета {target} больше нет --workers в CMD'
    return int(match.group(1))


def postgres_max_connections(service: str) -> int:
    """``max_connections``, заданный сервису PostgreSQL в compose ЯВНО.

    Умолчание postgres — тоже число (100), но ненаписанное число не с чем
    сверить, и именно так пулы одного из сервисов однажды сложились в 120
    соединений против него: увидеть это можно было только под нагрузкой, по
    ошибкам `too many connections`.
    """
    match = re.search(r'max_connections=(\d+)', compose_service(service))
    assert match, f'max_connections у {service} задан неявно — сверить его не с чем'
    return int(match.group(1))


def pool_ceiling(target: str, pool_key: str, overflow_key: str) -> int:
    """Сколько соединений может открыть КОНТЕЙНЕР, а не процесс.

    Ровно то расхождение, ради которого написан модуль: пул SQLAlchemy
    принадлежит воркеру uvicorn, а ``max_connections`` — базе, и между ними
    стоит множитель, которого не видно ни в одном из файлов, где записаны пулы.
    """
    return (env_example_int(pool_key) + env_example_int(overflow_key)) * uvicorn_workers(target)


def assert_pool_fits_database(
    target: str,
    database: str,
    pool_key: str,
    overflow_key: str,
    neighbours: int = 0,
) -> None:
    """Все воркеры контейнера плюс соседи по базе укладываются в её лимит.

    ``neighbours`` — потолок ЧУЖИХ соединений к той же базе (батч, разовые
    команды, миграции). Занять весь лимит горячим путём значило бы, что соседи
    не подключатся, — а их отказ выглядит как проблема совсем другого сервиса.

    Сообщения у assert'ов развёрнутые не для красоты: pytest переписывает
    assert'ы только в самих тестах, а этот модуль для него обычная библиотека,
    и без текста падение показало бы голое ``assert False``.
    """
    ceiling = pool_ceiling(target, pool_key, overflow_key) + neighbours
    available = postgres_max_connections(database) - SUPERUSER_RESERVED
    who = f'{target} и соседи по базе' if neighbours else target

    assert ceiling < available, (
        f'{who} могут открыть {ceiling} соединений к {database} '
        f'при доступных {available}: уменьшить {pool_key}/{overflow_key} '
        f'либо поднять max_connections {database}'
    )


def settings_default(settings_cls: type[BaseSettings], field: str) -> Any:
    """Умолчание, ОБЪЯВЛЕННОЕ в классе настроек, без единого чтения окружения.

    Не ``Settings(_env_file=None)``: этого мало. Отключить файл — не то же
    самое, что отключить переменные окружения, а nx подгружает корневой ``.env``
    разработчика в окружение задачи. Проверка кода тогда мерила содержимое чужой
    рабочей машины и молча зеленела на ней, падая у соседа: ровно на этом
    попались настройки ugc-api, где локальный ``.env`` ещё держал старые 10.
    """
    return settings_cls.model_fields[field].default


def assert_code_defaults_fit_database(
    target: str,
    database: str,
    pool_key: str,
    overflow_key: str,
    settings_cls: type[BaseSettings],
) -> None:
    """Умолчания ``Settings`` не свободнее шаблона и тоже влезают в лимит.

    Проверять обе пары обязательно: в образ копируется ``.env.example``, но
    ключ из него легко удалить — и тогда боевыми станут умолчания кода.

    Имена полей и ключей окружения совпадают у всех сервисов с UPPERCASE-стилем
    настроек, поэтому ``pool_key``/``overflow_key`` служат и тем и другим.
    """
    code_pool = settings_default(settings_cls, pool_key)
    code_overflow = settings_default(settings_cls, overflow_key)
    template_pool = env_example_int(pool_key)
    template_overflow = env_example_int(overflow_key)

    assert template_pool >= code_pool, (
        f'{pool_key} в .env.example ({template_pool}) строже умолчания кода ({code_pool}): '
        f'без этого ключа {target} поднимется с более широким пулом, чем задумано'
    )
    assert template_overflow >= code_overflow, (
        f'{overflow_key} в .env.example ({template_overflow}) строже умолчания кода '
        f'({code_overflow}): без этого ключа {target} поднимется с более широким пулом'
    )

    ceiling = (code_pool + code_overflow) * uvicorn_workers(target)
    available = postgres_max_connections(database) - SUPERUSER_RESERVED

    assert ceiling < available, (
        f'на умолчаниях кода {target} может открыть {ceiling} соединений к {database} при доступных {available}'
    )
