#!/usr/bin/env python3
"""Сверяет список `COPY <member>/pyproject.toml` в Dockerfile с членами workspace.

ЗАЧЕМ. Стадия `deps` копирует манифесты по одному и только потом ставит
зависимости — так слой с зависимостями выживает при правке исходников. Список
нельзя записать шаблоном (`COPY apps/*/pyproject.toml apps/` уплощает дерево и
схлопывает файлы в один), поэтому он перечислен вручную.

Ручной список гниёт молча. Добавили библиотеку, забыли строку — `uv sync
--locked` в стадии deps упадёт с внятной ошибкой... но только если новый пакет
кому-то нужен; если он пока не подключён, сборка пройдёт, а потом кто-то добавит
`COPY libs/ apps/` перед первым sync «чтобы починить», и слой зависимостей снова
начнёт инвалидироваться на каждую правку кода. Проверка ловит первый шаг.

Запуск: python tools/check_docker_manifests.py
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / 'infra' / 'docker' / 'python-service.Dockerfile'
ROOT_PYPROJECT = REPO_ROOT / 'pyproject.toml'


def workspace_members() -> set[str]:
    """Каталоги участников workspace, раскрытые из globs, минус исключения."""
    data = tomllib.loads(ROOT_PYPROJECT.read_text())
    workspace = data['tool']['uv']['workspace']
    excluded = set(workspace.get('exclude', []))

    members: set[str] = set()
    for pattern in workspace['members']:
        for path in sorted(REPO_ROOT.glob(pattern)):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if path.is_dir() and (path / 'pyproject.toml').exists() and rel not in excluded:
                members.add(rel)
    return members


def copied_manifests() -> set[str]:
    """Каталоги, чьи pyproject.toml копируются в стадии deps."""
    pattern = re.compile(r'^COPY\s+(\S+)/pyproject\.toml\s', re.MULTILINE)
    return set(pattern.findall(DOCKERFILE.read_text()))


def main() -> int:
    members = workspace_members()
    copied = copied_manifests()

    missing = members - copied
    extra = copied - members

    if not missing and not extra:
        print(f'OK: {len(members)} манифестов workspace перечислены в {DOCKERFILE.name}')
        return 0

    if missing:
        print('В Dockerfile НЕ копируются манифесты участников workspace:')
        for name in sorted(missing):
            print(f'  + COPY {name}/pyproject.toml {name}/')
    if extra:
        print('В Dockerfile копируются манифесты, которых нет в workspace:')
        for name in sorted(extra):
            print(f'  - {name}')
    return 1


if __name__ == '__main__':
    sys.exit(main())
