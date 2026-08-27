"""Тесты сборки HTML-отчёта по линтерам.

Проверяется то, что ломается молча: разбор трёх разных машинных форматов и
граница между «есть замечания» (норма, шаг зелёный) и «линтер не отработал»
(провал). Пустой отчёт при упавшем flake8 выглядит точно так же, как чистый
код, — именно поэтому неразобранная строка вывода становится ошибкой.
"""

import lint_report as lr

META = {'коммит': 'abc1234', 'ветка': 'main', 'прогон': '42'}


def report(key='wemake', findings=(), error=None):
    return lr.ToolReport(
        key=key,
        title=key,
        role='советник',
        command='flake8',
        findings=list(findings),
        error=error,
    )


def finding(path='libs/a.py', row=1, col=2, code='WPS110', text='Found wrong variable name'):
    return lr.Finding(path=path, row=row, col=col, code=code, text=text)


def test_flake8_output_is_parsed(monkeypatch):
    stdout = (
        'apps/auth/src/a.py|12|4|WPS432|Found magic number: 400\napps/auth/src/a.py|13|1|WPS420|Found wrong keyword\n'
    )
    monkeypatch.setattr(lr, 'run', lambda command: (1, stdout, ''))

    result = lr.collect_wemake()

    assert result.error is None
    assert result.status == 'dirty'
    assert [item.code for item in result.findings] == ['WPS432', 'WPS420']
    assert result.findings[0].text == 'Found magic number: 400'


def test_flake8_blank_trailing_line_is_not_an_error(monkeypatch):
    """flake8 завершает вывод переводом строки — это не сломанная строка."""
    monkeypatch.setattr(lr, 'run', lambda command: (1, 'libs/a.py|1|1|WPS110|Found wrong variable name\n\n', ''))

    result = lr.collect_wemake()

    assert result.error is None
    assert len(result.findings) == 1


def test_unparsable_flake8_line_is_a_failure(monkeypatch):
    """Трассировка упавшего плагина не должна превратиться в «замечаний нет»."""
    monkeypatch.setattr(lr, 'run', lambda command: (1, 'Traceback (most recent call last):\n', ''))

    result = lr.collect_wemake()

    assert result.status == 'broken'
    assert 'непонятная строка' in result.error


def test_tool_crash_is_a_failure(monkeypatch):
    """Код выхода вне {0, 1} — сбой самого линтера, а не результат проверки."""
    monkeypatch.setattr(lr, 'run', lambda command: (2, '', 'unknown option'))

    result = lr.collect_ruff()

    assert result.status == 'broken'
    assert result.error == 'unknown option'


def test_ruff_json_is_parsed(monkeypatch):
    stdout = """[{
        "filename": "tools/lint_report.py",
        "code": "UP032",
        "message": "Use f-string instead of `format` call",
        "location": {"row": 7, "column": 13}
    }]"""
    monkeypatch.setattr(lr, 'run', lambda command: (1, stdout, ''))

    result = lr.collect_ruff()

    assert result.error is None
    assert result.findings == [
        lr.Finding('tools/lint_report.py', 7, 13, 'UP032', 'Use f-string instead of `format` call')
    ]


def test_mypy_json_lines_are_parsed(monkeypatch):
    stdout = '{"file": "libs/platform-core/src/practix_core/jwt.py", "line": 30, "column": 8, "message": "Incompatible types", "code": "assignment", "severity": "error"}\n'
    monkeypatch.setattr(lr, 'run', lambda command: (1, stdout, ''))

    result = lr.collect_mypy()

    assert result.error is None
    assert result.findings[0].code == 'assignment'
    assert result.findings[0].row == 30


def test_clean_tool_reports_no_findings(monkeypatch):
    monkeypatch.setattr(lr, 'run', lambda command: (0, '', ''))

    assert lr.collect_ruff().status == 'clean'


def test_findings_are_escaped_in_html():
    """Текст замечания приходит из чужого вывода и попадает в разметку как есть."""
    text = lr.render([report(findings=[finding(text='Found `<script>` in "value"')])], META)

    assert '<script>' not in text
    assert '&lt;script&gt;' in text


def test_render_groups_by_file_worst_first():
    findings = [finding(path='libs/a.py'), finding(path='libs/b.py'), finding(path='libs/b.py')]

    text = lr.render([report(findings=findings)], META)

    assert text.index('libs/b.py') < text.index('libs/a.py')
    assert '<summary>libs/b.py <span class="n">— 2</span></summary>' in text


def test_broken_tool_is_visible_in_render():
    text = lr.render([report(error='flake8 не найден в PATH')], META)

    assert 'Линтер не отработал' in text
    assert 'flake8 не найден в PATH' in text


def test_step_summary_counts_every_tool():
    summary = lr.step_summary([report('ruff'), report('wemake', findings=[finding()]), report('mypy', error='упал')])

    assert '| ruff | 0 | чисто |' in summary
    assert '| wemake | 1 | есть замечания |' in summary
    assert '| mypy | н/д | линтер не отработал |' in summary
