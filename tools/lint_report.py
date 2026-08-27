#!/usr/bin/env python3
"""Собирает единый HTML-отчёт по трём линтерам: ruff, wemake-python-styleguide, mypy.

ЗАЧЕМ ОТЧЁТ, ЕСЛИ ЕСТЬ ГЕЙТЫ. Гейт отвечает на вопрос «можно ли мержить» и по
построению бинарен: `nx affected -t lint` либо зелёный, либо красный, и на
зелёном не остаётся ничего, что стоило бы читать. Отчёт отвечает на другой
вопрос — «где у нас долг и как он движется»: wemake-python-styleguide намеренно
не гейт (на существующем коде это ~1800 замечаний), и единственная форма, в
которой такой объём вообще можно смотреть, — сгруппированный по файлам документ,
который выгружается артефактом каждого прогона.

ЗАЧЕМ СКРИПТ, А НЕ `run:` В YAML — та же причина, что у tools/notify_telegram.py:
здесь есть разбор трёх разных машинных форматов и экранирование текста, который
пришёл из чужого вывода. В shell-блоке это код, который нельзя ни запустить
локально, ни прочитать в диффе, а ошибка в нём видна как пустой отчёт.

ЧТО СЧИТАЕТСЯ ПРОВАЛОМ. Найденные замечания — НЕ провал: скрипт выходит с нулём,
даже если их тысяча, иначе шаг сборки отчёта дублировал бы гейты и красил
прогон вторым цветом за то же самое. Ненулевой код возвращается только если
линтер не смог отработать (упал, не найден, отдал неразбираемый вывод) — то
есть когда отчёт врёт молчанием.

Отчёт самодостаточен: один HTML-файл без внешних стилей и скриптов, со светлой и
тёмной темой (артефакт открывают из браузера, а не из репозитория).

Запуск:
    uv run python tools/lint_report.py                 # -> .lint-report/index.html
    uv run python tools/lint_report.py --out build/x.html
    uv run python tools/lint_report.py --only wemake   # один линтер
"""

from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Пути для flake8: ему, в отличие от ruff и mypy, цель обхода не задаётся
# конфигом. Точка сюда не годится — flake8 пошёл бы в node_modules и .venv,
# полагаясь только на exclude, а список exclude и список целей разъезжаются.
FLAKE8_TARGETS = ('apps', 'libs', 'tools', 'seed_db.py')

# Формат вывода flake8 через плейсхолдеры — вместо JSON-плагина: у flake8 нет
# машинного формата в коробке, а лишняя зависимость ради одной строки не нужна.
# Разделитель '|' не встречается в кодах и путях, а текст сообщения идёт
# последним полем, поэтому split с ограничением не теряет '|' внутри него.
FLAKE8_FORMAT = '%(path)s|%(row)d|%(col)d|%(code)s|%(text)s'

# Коды выхода, при которых инструмент ОТРАБОТАЛ: 0 — чисто, 1 — есть замечания.
# Всё остальное (ruff 2, flake8 >1, mypy 2) — сбой самого инструмента.
OK_EXIT_CODES = frozenset({0, 1})


@dataclass(frozen=True, slots=True)
class Finding:
    """Одно замечание в общем для трёх линтеров виде."""

    path: str
    row: int
    col: int
    code: str
    text: str


@dataclass(slots=True)
class ToolReport:
    """Результат одного линтера: либо замечания, либо причина, почему их нет."""

    key: str
    title: str
    role: str
    command: str
    findings: list[Finding]
    error: str | None = None

    @property
    def status(self) -> str:
        if self.error:
            return 'broken'
        return 'dirty' if self.findings else 'clean'


def relative(path: str) -> str:
    """Путь относительно корня репозитория: абсолютные пути раннера читать нечем."""
    try:
        return str(Path(path).resolve().relative_to(REPO_ROOT))
    except ValueError:
        return path


def run(command: list[str]) -> tuple[int, str, str]:
    """Запускает линтер из корня репозитория; конфиги у всех трёх лежат там."""
    if shutil.which(command[0]) is None:
        return -1, '', f'{command[0]} не найден в PATH (нужен `uv sync --all-packages`)'
    completed = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True)
    return completed.returncode, completed.stdout, completed.stderr


def collect_ruff() -> ToolReport:
    """ruff — ГЕЙТ: его замечания обязательны к исправлению, в отчёте их быть не должно."""
    command = ['ruff', 'check', '.', '--output-format=json']
    report = ToolReport(
        key='ruff',
        title='ruff',
        role='гейт — падение ломает сборку',
        command=' '.join(command),
        findings=[],
    )
    code, stdout, stderr = run(command)
    if code not in OK_EXIT_CODES:
        report.error = stderr.strip() or f'код выхода {code}'
        return report
    try:
        raw = json.loads(stdout or '[]')
    except ValueError as exc:
        report.error = f'вывод не разобрался как JSON: {exc}'
        return report
    report.findings = [
        Finding(
            path=relative(item.get('filename', '?')),
            row=(item.get('location') or {}).get('row', 0),
            col=(item.get('location') or {}).get('column', 0),
            code=item.get('code') or 'ruff',
            text=item.get('message', ''),
        )
        for item in raw
    ]
    return report


def collect_wemake() -> ToolReport:
    """wemake-python-styleguide — СОВЕТНИК: список долга, а не условие мержа."""
    command = ['flake8', f'--format={FLAKE8_FORMAT}', *FLAKE8_TARGETS]
    report = ToolReport(
        key='wemake',
        title='wemake-python-styleguide',
        role='советник — не блокирует мерж',
        command=' '.join(command),
        findings=[],
    )
    code, stdout, stderr = run(command)
    if code not in OK_EXIT_CODES:
        report.error = stderr.strip() or f'код выхода {code}'
        return report
    findings = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split('|', 4)
        if len(parts) != 5:
            # Строка не по формату — это не замечание, а вывод самого flake8
            # (предупреждение о конфиге, трассировка плагина). Молча пропустить
            # её значило бы потерять единственный признак сломанного прогона.
            report.error = f'непонятная строка вывода: {line[:200]}'
            return report
        path, row, col, rule, text = parts
        findings.append(Finding(relative(path), int(row), int(col), rule, text))
    report.findings = findings
    return report


def collect_mypy() -> ToolReport:
    """mypy — ГЕЙТ по корневому конфигу (libs/), приложения подключаются по одному."""
    command = ['mypy', '--output=json']
    report = ToolReport(
        key='mypy',
        title='mypy',
        role='гейт — падение ломает сборку',
        command=' '.join(command),
        findings=[],
    )
    code, stdout, stderr = run(command)
    if code not in OK_EXIT_CODES:
        report.error = stderr.strip() or f'код выхода {code}'
        return report
    findings = []
    for line in stdout.splitlines():
        # `--output=json` даёт по объекту на строку; последняя строка сводки
        # («Found N errors») в этом режиме не печатается, но пустые — бывают.
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError as exc:
            report.error = f'строка вывода не разобралась как JSON: {exc}'
            return report
        findings.append(
            Finding(
                path=relative(item.get('file', '?')),
                row=item.get('line', 0) or 0,
                col=item.get('column', 0) or 0,
                code=item.get('code') or item.get('severity', 'error'),
                text=item.get('message', ''),
            )
        )
    report.findings = findings
    return report


COLLECTORS = {'ruff': collect_ruff, 'wemake': collect_wemake, 'mypy': collect_mypy}


def top_codes(findings: list[Finding], limit: int = 10) -> list[tuple[str, int]]:
    return Counter(finding.code for finding in findings).most_common(limit)


def by_file(findings: list[Finding]) -> list[tuple[str, list[Finding]]]:
    grouped: dict[str, list[Finding]] = defaultdict(list)
    for finding in findings:
        grouped[finding.path].append(finding)
    # Сначала самые проблемные файлы: отчёт читают сверху и до усталости.
    return sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))


STYLE = """
:root { color-scheme: light dark; --bg:#fff; --fg:#111; --muted:#5c6370; --line:#e3e6ea;
        --card:#f7f8fa; --clean:#1a7f37; --dirty:#9a6700; --broken:#cf222e; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#0d1117; --fg:#e6edf3; --muted:#9198a1; --line:#30363d;
          --card:#161b22; --clean:#3fb950; --dirty:#d29922; --broken:#f85149; }
}
* { box-sizing: border-box; }
body { margin:0; padding:2rem 1.25rem 4rem; background:var(--bg); color:var(--fg);
       font:15px/1.55 -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }
main { max-width: 1100px; margin: 0 auto; }
h1 { font-size:1.6rem; margin:0 0 .35rem; }
h2 { font-size:1.15rem; margin:2.5rem 0 .5rem; }
p.sub { color:var(--muted); margin:0 0 2rem; }
code, td.loc, .code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size:13px; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:.75rem; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:1rem; }
.card h3 { margin:0 0 .35rem; font-size:1rem; }
.card .count { font-size:2rem; font-weight:600; line-height:1.1; }
.card .role { color:var(--muted); font-size:13px; }
.clean { color:var(--clean); } .dirty { color:var(--dirty); } .broken { color:var(--broken); }
.wrap { overflow-x:auto; border:1px solid var(--line); border-radius:10px; margin:.75rem 0; }
table { border-collapse:collapse; width:100%; }
th, td { text-align:left; padding:.4rem .7rem; border-bottom:1px solid var(--line); vertical-align:top; }
th { position:sticky; top:0; background:var(--card); font-weight:600; }
tr:last-child td { border-bottom:none; }
td.loc { color:var(--muted); white-space:nowrap; }
details { border:1px solid var(--line); border-radius:10px; margin:.5rem 0; background:var(--card); }
details > summary { cursor:pointer; padding:.6rem .9rem; font-weight:600; }
details > summary .n { color:var(--muted); font-weight:400; }
details .wrap { border:none; border-top:1px solid var(--line); border-radius:0; margin:0; }
.note { background:var(--card); border:1px solid var(--line); border-left:3px solid var(--dirty);
        border-radius:8px; padding:.75rem 1rem; margin:.75rem 0; }
"""

STATUS_LABEL = {'clean': 'чисто', 'dirty': 'есть замечания', 'broken': 'линтер не отработал'}


def render(reports: list[ToolReport], meta: dict[str, str]) -> str:
    """Собирает самодостаточный HTML. Всё, что пришло из вывода линтеров, экранируется."""
    esc = html.escape
    out: list[str] = [
        '<!doctype html>',
        '<html lang="ru"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<title>Отчёт по линтерам — practix-cinema</title>',
        f'<style>{STYLE}</style></head><body><main>',
        '<h1>Отчёт по линтерам</h1>',
        '<p class="sub">{}</p>'.format(
            ' · '.join(f'{esc(key)}: <code>{esc(value)}</code>' for key, value in meta.items() if value)
        ),
        '<div class="cards">',
    ]
    for report in reports:
        count = 'н/д' if report.error else str(len(report.findings))
        status = report.status
        out.append(
            f'<div class="card"><h3>{esc(report.title)}</h3>'
            f'<div class="count {status}">{esc(count)}</div>'
            f'<div class="role">{esc(report.role)}</div>'
            f'<div class="role {status}">{esc(STATUS_LABEL[status])}</div></div>'
        )
    out.append('</div>')

    for report in reports:
        out.append(f'<h2 id="{esc(report.key)}">{esc(report.title)}</h2>')
        out.append(f'<p class="sub"><code>{esc(report.command)}</code></p>')
        if report.error:
            out.append(f'<div class="note broken">Линтер не отработал: <code>{esc(report.error)}</code></div>')
            continue
        if not report.findings:
            out.append('<div class="note clean">Замечаний нет.</div>')
            continue
        out.append('<div class="wrap"><table><thead><tr><th>Код</th><th>Замечаний</th></tr></thead><tbody>')
        for code, number in top_codes(report.findings):
            out.append(f'<tr><td class="code">{esc(code)}</td><td>{number}</td></tr>')
        out.append('</tbody></table></div>')
        for path, items in by_file(report.findings):
            out.append(
                f'<details><summary>{esc(path)} <span class="n">— {len(items)}</span></summary>'
                f'<div class="wrap"><table><tbody>'
            )
            for finding in sorted(items, key=lambda item: (item.row, item.col)):
                out.append(
                    f'<tr><td class="loc">{finding.row}:{finding.col}</td>'
                    f'<td class="code">{esc(finding.code)}</td>'
                    f'<td>{esc(finding.text)}</td></tr>'
                )
            out.append('</tbody></table></div></details>')

    out.append('</main></body></html>')
    return '\n'.join(out)


def step_summary(reports: list[ToolReport]) -> str:
    """Короткая сводка в «Summary» прогона: цифры видны без скачивания артефакта."""
    lines = ['### Отчёт по линтерам', '', '| Линтер | Замечаний | Статус |', '|---|---:|---|']
    for report in reports:
        count = 'н/д' if report.error else str(len(report.findings))
        lines.append(f'| {report.title} | {count} | {STATUS_LABEL[report.status]} |')
    return '\n'.join(lines) + '\n'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', default='.lint-report/index.html', help='куда положить HTML')
    parser.add_argument('--only', choices=sorted(COLLECTORS), action='append', help='запустить только эти линтеры')
    args = parser.parse_args()

    selected = args.only or list(COLLECTORS)
    reports = [COLLECTORS[key]() for key in COLLECTORS if key in selected]

    meta = {
        'коммит': os.environ.get('GITHUB_SHA', '')[:7],
        'ветка': os.environ.get('GITHUB_REF_NAME', ''),
        'прогон': os.environ.get('GITHUB_RUN_ID', ''),
    }
    destination = Path(args.out)
    if not destination.is_absolute():
        destination = REPO_ROOT / destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render(reports, meta), encoding='utf-8')

    for report in reports:
        state = report.error or f'{len(report.findings)} замечаний'
        print(f'{report.title}: {state}')
    print(f'Отчёт: {destination}')

    summary_path = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary_path:
        with open(summary_path, 'a', encoding='utf-8') as handle:
            handle.write(step_summary(reports))

    # Замечания — не провал шага (см. докстринг). Провал — только сломанный линтер.
    broken = [report.title for report in reports if report.error]
    if broken:
        print(f'Не отработали: {", ".join(broken)}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
