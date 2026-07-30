"""Тесты сборки сообщения об итоге CI.

Проверяется ровно то, что в shell-варианте этого шага проверить было нечем:
какой прогон считается успешным, и что текст от автора PR не ломает разбор HTML
на стороне Telegram (ответ 400 вместо сообщения — единственный симптом).
"""

import aiohttp
import notify_telegram as nt

ENV = {
    'GITHUB_REPOSITORY': 'user/practix-cinema',
    'GITHUB_REF_NAME': 'main',
    'GITHUB_SHA': '015c8f6abcdef0123456789',
    'GITHUB_ACTOR': 'ruslanivanov',
    'GITHUB_SERVER_URL': 'https://github.com',
    'GITHUB_RUN_ID': '42',
    'COMMIT_MESSAGE': 'Настроить CI',
}

ALL_GREEN = {
    'checks': 'success',
    'affected-suites': 'success',
    'duplication': 'success',
    'functional': 'success',
}


def test_all_green_is_success():
    text = nt.build_message(ALL_GREEN, ENV)

    assert text.startswith('<b>✅ CI прошёл — user/practix-cinema</b>')
    assert 'Итог: <b>успешно</b>' in text
    assert '<code>015c8f6</code>' in text
    assert 'https://github.com/user/practix-cinema/actions/runs/42' in text


def test_link_falls_back_to_actions_list_without_run_id():
    """Без GITHUB_RUN_ID (запуск руками) `.../actions/runs/` отдаёт 404."""
    env = ENV | {'GITHUB_RUN_ID': ''}

    text = nt.build_message(ALL_GREEN, env)

    assert 'href="https://github.com/user/practix-cinema/actions"' in text


def test_failed_job_makes_run_failed():
    text = nt.build_message({**ALL_GREEN, 'duplication': 'failure'}, ENV)

    assert text.startswith('<b>❌ CI упал')
    assert 'Итог: <b>с ошибкой</b>' in text
    assert '❌ дублирование кода' in text


def test_skipped_is_not_a_failure():
    """Функциональные наборы не запускаются, если affected их не выбрал."""
    text = nt.build_message({**ALL_GREEN, 'functional': 'skipped'}, ENV)

    assert 'Итог: <b>успешно</b>' in text
    assert '⏭ функциональные тесты' in text


def test_unknown_result_counts_as_failure():
    """Незнакомый статус — повод разобраться, а не молча считать прогон зелёным."""
    text = nt.build_message({**ALL_GREEN, 'checks': 'timed_out'}, ENV)

    assert 'Итог: <b>с ошибкой</b>' in text
    assert '❌ проверки' in text


def test_commit_subject_is_escaped_and_single_line():
    """Заголовок коммита пишет автор PR: '<' в нём ломает parse_mode=HTML."""
    env = ENV | {'COMMIT_MESSAGE': 'Fix <b>bug</b> & more\n\nПодробности в теле'}

    text = nt.build_message(ALL_GREEN, env)

    assert 'Fix &lt;b&gt;bug&lt;/b&gt; &amp; more' in text
    assert 'Подробности' not in text


def test_pull_request_falls_back_to_title():
    """В событии pull_request head_commit пуст — заголовок берётся у PR."""
    env = ENV | {'COMMIT_MESSAGE': '', 'PR_TITLE': 'Добавить уведомление'}

    assert 'Добавить уведомление' in nt.build_message(ALL_GREEN, env)


def test_unknown_job_is_shown_under_its_own_name():
    """Новая job в workflow должна попасть в сообщение сама, а не потеряться."""
    text = nt.build_message({**ALL_GREEN, 'security-scan': 'failure'}, ENV)

    assert '❌ security-scan' in text


def test_order_does_not_depend_on_input_order():
    """Порядок ключей в toJSON(needs) задаёт GitHub, читаться должно одинаково."""
    shuffled = dict(reversed(list(ALL_GREEN.items())))

    assert nt.build_message(shuffled, ENV) == nt.build_message(ALL_GREEN, ENV)


def test_client_errors_are_not_retried():
    """401/400 — неверный токен или чат: повтор только задержит шаг на 15 секунд."""
    assert not nt.retriable(nt.TelegramError(401, '{"description": "Unauthorized"}'))
    assert not nt.retriable(nt.TelegramError(400, '{"description": "chat not found"}'))


def test_transient_failures_are_retried():
    assert nt.retriable(nt.TelegramError(429, '{"description": "Too Many Requests"}'))
    assert nt.retriable(nt.TelegramError(502, ''))
    assert nt.retriable(aiohttp.ClientError('сеть раннера отвалилась'))
    assert nt.retriable(TimeoutError())


def test_ok_false_with_status_200_is_a_failure():
    """Telegram отвечает 200 с `{"ok": false}` — по коду такой отказ не виден."""
    error = nt.TelegramError(200, '{"ok": false, "description": "bot was blocked"}')

    assert 'bot was blocked' in str(error)
    assert nt.retriable(error) is False


def test_job_results_parses_needs_context():
    raw = '{"checks": {"result": "success", "outputs": {}}, "functional": {"result": "skipped"}}'

    assert nt.job_results(raw) == {'checks': 'success', 'functional': 'skipped'}


def test_job_results_on_empty_context():
    assert nt.job_results('') == {}
