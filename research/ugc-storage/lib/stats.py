"""
Статистика замеров и вывод таблиц.

Среднее по латентности бесполезно: оно прячет ровно тот хвост, из-за которого
пользователь видит подвисание. Требование задания сформулировано как «200 мс»,
и отвечать на него нужно перцентилями, а не средним.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass

# Целевое время обработки из задания.
SLA_MS = 200.0


@dataclass
class Measurement:
    scenario: str
    description: str
    samples: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    avg_ms: float
    rps: float
    errors: int

    @property
    def within_sla(self) -> bool:
        # Сценарий с ошибками не «уложился» ни во что: падающий запрос
        # отрабатывает мгновенно, и без этой проверки полностью сломанный
        # сценарий попадал в отчёт как самый быстрый. Ровно так и случилось на
        # первом прогоне ClickHouse.
        return self.errors == 0 and self.p99_ms <= SLA_MS

    def as_dict(self) -> dict:
        data = asdict(self)
        data['within_sla'] = self.within_sla
        return data


def summarize(
    scenario: str,
    description: str,
    durations_ms: list[float],
    wall_seconds: float,
    errors: int = 0,
) -> Measurement:
    if not durations_ms:
        return Measurement(scenario, description, 0, 0, 0, 0, 0, 0, 0, errors)

    ordered = sorted(durations_ms)
    # quantiles(n=100) требует минимум 100 наблюдений; на малых прогонах
    # (--iterations 20 в smoke) берём индекс напрямую, иначе скрипт падал бы
    # именно там, где его запускают в первый раз.
    if len(ordered) >= 100:
        cuts = statistics.quantiles(ordered, n=100, method='inclusive')
        p50, p95, p99 = cuts[49], cuts[94], cuts[98]
    else:
        p50 = ordered[int(len(ordered) * 0.50)] if len(ordered) > 1 else ordered[0]
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        p99 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))]

    return Measurement(
        scenario=scenario,
        description=description,
        samples=len(ordered),
        p50_ms=round(p50, 2),
        p95_ms=round(p95, 2),
        p99_ms=round(p99, 2),
        max_ms=round(ordered[-1], 2),
        avg_ms=round(statistics.fmean(ordered), 2),
        rps=round(len(ordered) / wall_seconds, 1) if wall_seconds > 0 else 0.0,
        errors=errors,
    )


def markdown_table(measurements: list[Measurement]) -> str:
    """Готовая для вставки в README таблица.

    Колонка «200 мс» есть потому, что именно на этот вопрос отвечает
    исследование: числа без вердикта читателю пришлось бы сравнивать глазами.
    """
    header = (
        '| Сценарий | Что измеряется | N | p50, мс | p95, мс | p99, мс | max, мс | RPS | Ошибок | 200 мс |\n'
        '|---|---|---:|---:|---:|---:|---:|---:|---:|:--:|'
    )
    rows = [
        f'| `{m.scenario}` | {m.description} | {m.samples} | {m.p50_ms} | {m.p95_ms} | '
        f'{m.p99_ms} | {m.max_ms} | {m.rps} | {m.errors} | {"да" if m.within_sla else "**нет**"} |'
        for m in measurements
    ]
    return '\n'.join([header, *rows])


def print_table(measurements: list[Measurement]) -> None:
    print()
    print(markdown_table(measurements))
    print()
    broken = [m for m in measurements if m.errors]
    if broken:
        print('ОШИБКИ (результат недействителен): ' + ', '.join(f'{m.scenario} — {m.errors}' for m in broken))
    slow = [m for m in measurements if not m.errors and m.p99_ms > SLA_MS]
    if slow:
        print(f'Не уложились в {SLA_MS:.0f} мс по p99: ' + ', '.join(m.scenario for m in slow))
    elif not broken:
        print(f'Все сценарии уложились в {SLA_MS:.0f} мс по p99.')
