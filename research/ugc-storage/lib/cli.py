"""
Разбор аргументов и чтение .env — общее для generate.py и benchmark.py.

argparse здесь сознательно не используется: в репозитории нет ни одного скрипта
с CLI-фреймворком (seed_db.py, tools/*.py разбирают sys.argv руками), и заводить
его ради шести флагов означало бы выбиваться из стиля без выигрыша.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

STAND_ROOT = Path(__file__).resolve().parents[1]


def load_env() -> None:
    """Читает research/ugc-storage/.env, если он есть.

    Без внешнего python-dotenv: у стенда и так три опциональных драйвера,
    четвёртая зависимость ради пятнадцати строк парсера — перебор. Уже
    выставленные переменные окружения приоритетнее файла.
    """
    env_file = STAND_ROOT / '.env'
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        os.environ.setdefault(key.strip(), value.strip())


def parse_args(spec: dict[str, object], usage: str) -> dict:
    """Разбирает `--ключ значение` и `--флаг` по типам значений из `spec`.

    Тип берётся из значения по умолчанию: bool — флаг без аргумента, int —
    число, остальное — строка.
    """
    values = dict(spec)
    argv = sys.argv[1:]
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in ('-h', '--help'):
            print(usage)
            raise SystemExit(0)
        if not token.startswith('--'):
            raise SystemExit(f'Не понимаю аргумент: {token}\n\n{usage}')
        key = token[2:].replace('-', '_')
        if key not in values:
            raise SystemExit(f'Неизвестный флаг: {token}\n\n{usage}')
        default = spec[key]
        if isinstance(default, bool):
            values[key] = True
            index += 1
            continue
        index += 1
        if index >= len(argv):
            raise SystemExit(f'У флага {token} нет значения\n\n{usage}')
        values[key] = int(argv[index]) if isinstance(default, int) else argv[index]
        index += 1
    return values


def bootstrap(spec: dict[str, object], usage: str):
    """Разбор аргументов и сборка тройки «профиль — датасет — хранилище».

    Общая часть generate.py и benchmark.py. Вынесена сюда не только ради
    краткости: проверка `--scale` обязана быть одна на два скрипта, иначе легко
    залить данные одним профилем, а померить другим — и получить сравнение
    хранилищ на разных объёмах, не заметив этого.
    """
    from lib.dataset import PROFILES, Dataset
    from lib.store import build_store

    load_env()
    args = parse_args(spec, usage)
    if not args['store']:
        raise SystemExit(f'Не указан --store\n\n{usage}')
    if args['scale'] not in PROFILES:
        raise SystemExit(f'Неизвестный --scale: {args["scale"]}. Доступны: {", ".join(PROFILES)}')

    profile = PROFILES[args['scale']]
    return args, profile, Dataset(profile, seed=args['seed']), build_store(args['store'])


def human(number: float) -> str:
    return f'{number:,.0f}'.replace(',', ' ')
