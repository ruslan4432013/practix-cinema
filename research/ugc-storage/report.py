"""
Сборка результатов трёх прогонов в таблицы для README.

Отдельный скрипт, а не часть benchmark.py: прогоны идут по одному, каждый со
своим поднятым профилем, и свести их можно только после последнего. Драйверов
не требует — читает только JSON из results/.

Запуск:

    uv run python research/ugc-storage/report.py            # профиль full
    uv run python research/ugc-storage/report.py --scale smoke
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.cli import parse_args
from lib.stats import SLA_MS

USAGE = __doc__ or ''

STORES = [('mongo', 'MongoDB'), ('postgres', 'PostgreSQL'), ('clickhouse', 'ClickHouse')]
RESULTS = Path(__file__).resolve().parent / 'results'


def cell(measurement: dict | None, metric: str) -> str:
    if not measurement:
        return '—'
    # Упавший сценарий отрабатывает мгновенно, и его перцентили выглядят
    # прекрасно. Показывать их рядом с честными числами нельзя.
    if measurement.get('errors'):
        return f'ошибки ({measurement["errors"]})'
    value = measurement[metric]
    marker = '' if value <= SLA_MS else ' ⚠️'
    return f'{value:.1f}{marker}'


def main() -> int:
    args = parse_args({'scale': 'full'}, USAGE)
    reports = {}
    for key, _ in STORES:
        path = RESULTS / f'{key}-{args["scale"]}.json'
        if path.exists():
            reports[key] = json.loads(path.read_text(encoding='utf-8'))

    if not reports:
        raise SystemExit(f'В {RESULTS} нет результатов профиля {args["scale"]}')

    present = [(key, title) for key, title in STORES if key in reports]
    missing = [title for key, title in STORES if key not in reports]
    if missing:
        print(f'<!-- нет результатов: {", ".join(missing)} -->\n')

    # Порядок сценариев берём из первого отчёта: он задан в lib/store.py и
    # одинаков для всех прогонов.
    first = reports[present[0][0]]['measurements']
    scenarios = [(m['scenario'], m['description']) for m in first]
    by_store = {key: {m['scenario']: m for m in reports[key]['measurements']} for key, _ in present}

    for metric, title in (('p99_ms', 'p99'), ('p50_ms', 'p50')):
        print(f'### {title}, мс\n')
        print('| Сценарий | Что измеряется | ' + ' | '.join(t for _, t in present) + ' |')
        print('|---|---|' + '---:|' * len(present))
        for code, description in scenarios:
            values = [cell(by_store[key].get(code), metric) for key, _ in present]
            print(f'| `{code}` | {description} | ' + ' | '.join(values) + ' |')
        print()

    meta = reports[present[0][0]]
    print(
        f'Профиль `{meta["profile"]["name"]}`, {meta["iterations"]} измерений на сценарий, '
        f'параллельность {meta["concurrency"]}, {meta["platform"]}, Python {meta["python"]}. '
        f'⚠️ — выход за {SLA_MS:.0f} мс.'
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
