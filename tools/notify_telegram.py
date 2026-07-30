#!/usr/bin/env python3
"""Отправляет итог прогона CI в Telegram.

ЗАЧЕМ ОТДЕЛЬНЫЙ СКРИПТ, А НЕ `run: curl ...` В WORKFLOW. Сообщение собирается из
результатов ЧЕТЫРЁХ job, а текст приходит извне (заголовок коммита пишет автор
PR), поэтому в шаге появляются ровно те две вещи, которые в YAML не проверить
ничем: агрегация статусов и экранирование. В shell-блоке они превращаются в цикл
с функциями внутри строки YAML — код, который нельзя ни запустить локально, ни
покрыть тестом, а ошибка в нём видна только по молчащему боту.

Здесь логика — обычный Python рядом с остальными проверками из `tools/`:
запускается локально (`--dry-run`), падает на несобранном сообщении, а не на
проде.

HTTP — на `aiohttp`, как во всех сервисах репозитория: один и тот же клиент и
одна и та же модель таймаутов везде, где ходят по сети. Единственная внешняя
зависимость скрипта, поэтому в CI он запускается как
`uv run --no-project --with aiohttp` — окружение workspace ради одного запроса
ставить незачем.

Данные берутся из окружения, а НЕ из подстановок `${{ }}` в тело скрипта:
подстановка заголовка коммита в исполняемый текст — это выполнение чужой строки
раннером.

    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   секреты репозитория; без них скрипт
                                           молча выходит с 0 (в PR из форка
                                           секретов нет по построению)
    CI_NEEDS                               `${{ toJSON(needs) }}` — результаты job
    COMMIT_MESSAGE / PR_TITLE              заголовок для сообщения
    GITHUB_*                               стандартные переменные раннера

Запуск:
    uv run python tools/notify_telegram.py
    uv run python tools/notify_telegram.py --dry-run   # напечатать, не отправляя
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import sys

import aiohttp

API_URL = 'https://api.telegram.org/bot{token}/sendMessage'
SEND_ATTEMPTS = 3
SEND_TIMEOUT = 30

# Человеческие названия job. Ключи обязаны совпадать с именами в ci.yml: незнакомая
# job попадёт в сообщение под своим именем, а не потеряется.
JOB_TITLES = {
    'checks': 'проверки (линтер, типы, юнит-тесты)',
    'affected-suites': 'выбор затронутых наборов',
    'duplication': 'дублирование кода',
    'functional': 'функциональные тесты',
}

# `skipped` — НОРМА, а не провал: функциональные наборы не запускаются, если
# `nx affected` не выбрал ни одного. Всё, что не success и не skipped, — провал.
OK_RESULTS = frozenset({'success', 'skipped'})
ICONS = {'success': '✅', 'skipped': '⏭', 'cancelled': '⏹'}
FAILURE_ICON = '❌'


def job_results(raw: str) -> dict[str, str]:
    """Разбирает `toJSON(needs)`: {"checks": {"result": "success", ...}, ...}."""
    if not raw.strip():
        return {}
    return {name: data.get('result', 'unknown') for name, data in json.loads(raw).items()}


def ordered(results: dict[str, str]) -> list[tuple[str, str]]:
    """Порядок строк в сообщении — как в JOB_TITLES, незнакомые job в конец.

    Порядок ключей в `toJSON(needs)` задаёт GitHub, и опираться на него нельзя:
    сообщение об одном и том же прогоне читалось бы каждый раз по-новому.
    """
    order = list(JOB_TITLES)
    return sorted(results.items(), key=lambda item: (order.index(item[0]) if item[0] in order else len(order), item[0]))


def build_message(results: dict[str, str], env: dict[str, str]) -> str:
    """Собирает текст для parse_mode=HTML.

    Экранируется ВСЁ, что пришло извне: '<' в заголовке коммита ломает разбор, и
    Telegram отвечает 400 вместо отправки.
    """
    failed = [name for name, result in results.items() if result not in OK_RESULTS]
    header = f'{FAILURE_ICON} CI упал' if failed else '✅ CI прошёл'
    overall = 'с ошибкой' if failed else 'успешно'

    # Первая строка сообщения коммита; в pull_request событии head_commit пуст.
    subject = (env.get('COMMIT_MESSAGE') or env.get('PR_TITLE') or '').strip().splitlines()
    repo = env.get('GITHUB_REPOSITORY', '?')
    sha = env.get('GITHUB_SHA', '')
    # GITHUB_RUN_ID подставляет раннер. Пустым он бывает только при запуске руками
    # — и тогда `.../actions/runs/` даёт 404. Ссылка на список прогонов в этом
    # случае бесполезнее, но не врёт.
    actions_url = '{server}/{repo}/actions'.format(
        server=env.get('GITHUB_SERVER_URL', 'https://github.com'),
        repo=repo,
    )
    run_id = env.get('GITHUB_RUN_ID', '')
    run_url = f'{actions_url}/runs/{run_id}' if run_id else actions_url

    lines = [
        f'<b>{html.escape(header)} — {html.escape(repo)}</b>',
        'Ветка: <code>{branch}</code> · коммит <code>{sha}</code>'.format(
            branch=html.escape(env.get('GITHUB_REF_NAME', '?')),
            sha=html.escape(sha[:7]),
        ),
        f'Автор: {html.escape(env.get("GITHUB_ACTOR", "?"))}',
    ]
    if subject:
        lines.append(html.escape(subject[0]))
    lines.append('')
    lines += [f'{ICONS.get(result, FAILURE_ICON)} {JOB_TITLES.get(name, name)}' for name, result in ordered(results)]
    lines.append('')
    lines.append(f'Итог: <b>{overall}</b> · <a href="{html.escape(run_url, quote=True)}">лог прогона</a>')
    return '\n'.join(lines)


class TelegramError(RuntimeError):
    """Отказ Telegram: HTTP-код и тело ответа.

    Отдельный тип, потому что решение о повторе принимается по КОДУ, а сам код
    приходит двумя разными путями: обычным HTTP-статусом и полем `ok` в теле —
    Telegram умеет ответить 200 с `{"ok": false}`.
    """

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f'HTTP {status}: {body}')
        self.status = status


async def send(token: str, chat_id: str, text: str) -> None:
    """Шлёт сообщение, повторяя попытки: сеть раннера — не гарантия."""
    payload = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'HTML',
        'disable_web_page_preview': 'true',
    }
    timeout = aiohttp.ClientTimeout(total=SEND_TIMEOUT)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for attempt in range(1, SEND_ATTEMPTS + 1):
            try:
                await post(session, API_URL.format(token=token), payload)
                return
            except (TelegramError, aiohttp.ClientError, TimeoutError) as exc:
                print(f'Попытка {attempt}/{SEND_ATTEMPTS} не удалась: {exc}')
                # Повторяются только сбои, которые могут пройти сами: сеть, 5xx и
                # 429. Неверный токен или чат — это 401/400, и три попытки по
                # нарастающей паузе лишь задерживают шаг, ничего не меняя.
                if attempt == SEND_ATTEMPTS or not retriable(exc):
                    raise
                await asyncio.sleep(5 * attempt)


async def post(session: aiohttp.ClientSession, url: str, payload: dict[str, str]) -> None:
    async with session.post(url, data=payload) as response:
        # Тело читается ДО проверки статуса: причину отказа («chat not found»,
        # «bot was blocked by the user») Telegram описывает именно в нём, а не в
        # коде — по одному 400 понять, что чинить, невозможно.
        body = await response.text()
        try:
            ok = bool(json.loads(body).get('ok'))
        except ValueError:
            ok = False
        if response.status != 200 or not ok:
            raise TelegramError(response.status, body)


def retriable(exc: Exception) -> bool:
    if isinstance(exc, TelegramError):
        return exc.status == 429 or exc.status >= 500
    # Сеть и таймауты — повторяем: у раннера они разовые.
    return True


async def main() -> int:
    dry_run = '--dry-run' in sys.argv[1:]
    env = dict(os.environ)

    token = env.get('TELEGRAM_BOT_TOKEN', '')
    chat_id = env.get('TELEGRAM_CHAT_ID', '')

    results = job_results(env.get('CI_NEEDS', ''))
    if not results:
        print('CI_NEEDS пуст — нечего сообщать. Ожидается `${{ toJSON(needs) }}`.')
        return 1

    text = build_message(results, env)

    if dry_run:
        print(text)
        return 0

    if not token or not chat_id:
        # Не провал: в PR из форка секреты недоступны по построению, и падать на
        # этом CI не должен.
        print('::notice::TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не заданы — сообщение не отправлено.')
        return 0

    try:
        await send(token, chat_id, text)
    except Exception as exc:  # noqa: BLE001 — сообщение об ошибке важнее её типа
        # Шаг падает намеренно: молчащее уведомление хуже отсутствующего, о нём
        # никто не узнает до следующего разбора «а почему бот молчит».
        print(f'Не удалось отправить сообщение в Telegram: {exc}')
        return 1

    print('Сообщение отправлено.')
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
