#!/usr/bin/env python3
"""Сверяет рёбра графа Nx с ожидаемым набором.

ЗАЧЕМ ЭТО ОТДЕЛЬНАЯ ПРОВЕРКА. Рёбра `lib -> app` строит `@nxlv/python` из ТРЁХ
источников одновременно: строки зависимости в `[project].dependencies`, записи
`{ workspace = true }` в корневом `[tool.uv.sources]` и метаданных `uv.lock`
(`package.<app>.metadata.requires-dist.<dep>.editable`). Достаточно одному из них
отстать — и рёбра ПРОПАДАЮТ МОЛЧА. Граф остаётся валидным, `nx affected`
продолжает работать и просто перестаёт выбирать приложения при правке библиотеки:
изменение общего кода уезжает непротестированным.

Это единственный сценарий, в котором монорепозиторий становится ХУЖЕ того, что
было до него, поэтому проверка декларативная, а не «посмотрим глазами в nx graph».

Запуск:
    npx nx graph --file=/tmp/graph.json
    python tools/check_graph_edges.py /tmp/graph.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Ожидаемые зависимости: проект -> множество проектов, от которых он зависит.
# Правится ОСОЗНАННО вместе с pyproject.toml соответствующего приложения.
EXPECTED: dict[str, set[str]] = {
    'movies-api': {'platform-core', 'testing'},
    'auth': {'platform-core', 'testing'},
    'analytics-collector': {'platform-core', 'analytics-contracts', 'testing'},
    'etl-clickhouse': {'platform-core', 'analytics-contracts', 'testing'},
    'ugc-api': {'platform-core', 'testing'},
    'notifications': {'platform-core', 'testing'},
    # Без ребра на testing: функциональных тестов у шлюза своих НЕТ. Сквозной
    # путь (событие → веер → сборка → push → сокет) проверяется набором
    # нотификаций, где уже стоит весь стенд, — вторая копия его conftest.py
    # (450 строк обвязки) стала бы крупнейшим дублированием в репозитории.
    'notifications-ws': {'platform-core'},
    'link-shortener': {'platform-core', 'testing'},
    'recommendations-api': {'platform-core', 'testing'},
    # Ребро на testing есть и у батча: его функциональный набор ждёт готовности
    # ClickHouse скриптом practix_testing.utils.wait_for_clickhouse, а не
    # собственным циклом опроса — третьей копией того же ожидания.
    'recsys-trainer': {'platform-core', 'testing'},
    'etl-elasticsearch': {'platform-core', 'search-schema'},
    'analytics-contracts': set(),
    'platform-core': set(),
    'search-schema': set(),
    'testing': {'platform-core', 'search-schema'},
    # django-admin вне workspace (Python 3.12, свой lock) — рёбер нет и быть не должно.
    'django-admin': set(),
    # tools — проверки для CI: не пакет и не член workspace, объявлен project.json
    # ради целей lint/test. Рёбер нет: на библиотеки репозитория скрипты не
    # опираются (единственная внешняя зависимость — aiohttp в уведомлении).
    'tools': set(),
}


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2

    graph = json.loads(Path(sys.argv[1]).read_text())['graph']
    deps = graph['dependencies']

    actual: dict[str, set[str]] = {}
    for project in graph['nodes']:
        actual[project] = {d['target'] for d in deps.get(project, []) if d['target'] in graph['nodes']}

    problems: list[str] = []

    unexpected_projects = set(actual) - set(EXPECTED)
    if unexpected_projects:
        problems.append(
            f'Проекты есть в графе, но нет в ожиданиях: {sorted(unexpected_projects)}.\n'
            '  Добавь их в EXPECTED этого файла.'
        )
    for project in sorted(set(EXPECTED) & set(actual)):
        want, got = EXPECTED[project], actual[project]
        if missing := want - got:
            problems.append(
                f'{project}: ПОТЕРЯНЫ рёбра на {sorted(missing)}.\n'
                '  Скорее всего устарел uv.lock — запусти `uv lock` и проверь, что в\n'
                '  pyproject.toml проекта есть зависимость, а в корневом [tool.uv.sources]\n'
                '  — запись `{ workspace = true }`.'
            )
        if unexpected := got - want:
            problems.append(f'{project}: ЛИШНИЕ рёбра на {sorted(unexpected)}.')

    if problems:
        print('Граф зависимостей не совпадает с ожидаемым:\n')
        for p in problems:
            print(f'- {p}')
        return 1

    total = sum(len(v) for v in actual.values())
    print(f'OK: {len(actual)} проектов, {total} рёбер — совпадает с ожидаемым')
    return 0


if __name__ == '__main__':
    sys.exit(main())
