"""Шаблонизатор писем и валидация шаблонов менеджера.

Чистый модуль: Django здесь нет, поэтому и форма админки, и приёмник событий, и
воркер пользуются ОДНОЙ реализацией, а тесты не требуют настроенного фреймворка.

## Зачем вообще валидировать

Теория («Как устроен почтальон Печкин») отвечает на это прямо: шаблон пишет
менеджер — без образования разработчика и без знания устройства системы. Без
проверки он способен сохранить шаблон, который упадёт при сборке письма или уйдёт
в вечный цикл, и узнаем мы об этом на рассылке в сто тысяч адресов. Проверок
ровно три, в том же порядке:

1. синтаксис шаблона;
2. использованы только доступные менеджеру возможности;
3. письмо в принципе рендерится.

## Чем ограничены возможности

``SandboxedEnvironment`` закрывает доступ к атрибутам и методам объектов
(``{{ x.__class__ }}`` и подобное). Сверх него ОЧИЩАЮТСЯ ГЛОБАЛЫ окружения
(``range``, ``dict``, ``cycler``, ``joiner``, ``lipsum``, ``namespace``): без
``range`` в шаблоне нечего итерировать, кроме переменных из белого списка, а их
размер задаём мы — то есть «вечный цикл» перестаёт быть достижимым, а не просто
маловероятным. ``StrictUndefined`` превращает опечатку в имени переменной в
ошибку валидации вместо тихо пустого места в письме.

## Про таймаут рендера

``render_guarded`` выполняет рендер в отдельном процессе и убивает его по
таймауту — поточный таймаут бесполезен, зациклившийся рендер не прерывается.
Используется он в ВАЛИДАЦИИ (сохранение шаблона, приём события): там это
происходит редко и стоимость запуска процесса не важна. Путь отправки рендерит
напрямую: шаблон туда попадает уже проверенным и с очищенными глобалами, а
процесс на каждое письмо стоил бы дороже самой отправки.
"""

import multiprocessing
from typing import Any

from jinja2 import StrictUndefined, TemplateError, meta
from jinja2.sandbox import SandboxedEnvironment

#: Глобалы Jinja2, которые менеджеру недоступны. Очищается всё окружение целиком,
#: список — для сообщения об ошибке и для теста.
STRIPPED_GLOBALS = ('range', 'dict', 'lipsum', 'cycler', 'joiner', 'namespace')


class TemplateValidationError(Exception):
    """Шаблон не прошёл проверку. ``errors`` — список человекочитаемых причин."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__('; '.join(errors))


class TemplateRenderTimeout(TemplateValidationError):
    def __init__(self, seconds: float):
        super().__init__([f'Рендер шаблона не уложился в {seconds} с — вероятно, бесконечный цикл'])


def build_environment(*, is_html: bool) -> SandboxedEnvironment:
    """Песочница для шаблонов менеджера.

    ``autoescape`` включается только для HTML: в текстовом письме экранирование
    превратило бы кавычки в ``&quot;`` прямо в теле.
    """
    env = SandboxedEnvironment(autoescape=is_html, undefined=StrictUndefined)
    env.globals.clear()
    return env


def find_variables(source: str, *, is_html: bool = True) -> set[str]:
    """Имена переменных, которые шаблон ожидает снаружи."""
    env = build_environment(is_html=is_html)
    return set(meta.find_undeclared_variables(env.parse(source)))


def render(source: str, context: dict[str, Any], *, is_html: bool = True) -> str:
    """Собрать текст. Бросает ``jinja2.TemplateError`` при любой проблеме."""
    return build_environment(is_html=is_html).from_string(source).render(**context)


def _render_child(conn, source: str, context: dict[str, Any], is_html: bool) -> None:
    try:
        conn.send(('ok', render(source, context, is_html=is_html)))
    except BaseException as exc:  # noqa: BLE001 - в дочернем процессе некому разбирать типы
        conn.send(('error', f'{type(exc).__name__}: {exc}'))
    finally:
        conn.close()


def render_guarded(source: str, context: dict[str, Any], *, is_html: bool, timeout: float) -> str:
    """Рендер в отдельном процессе с жёстким таймаутом.

    ``spawn``, а не ``fork``: процесс-родитель держит открытые сокеты Postgres и
    брокера, и наследовать их ребёнку незачем; заодно поведение одинаково на
    Linux и macOS.
    """
    ctx = multiprocessing.get_context('spawn')
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_render_child, args=(child_conn, source, context, is_html), daemon=True)
    process.start()
    child_conn.close()
    try:
        if not parent_conn.poll(timeout):
            process.terminate()
            raise TemplateRenderTimeout(timeout)
        status, payload = parent_conn.recv()
    finally:
        parent_conn.close()
        process.join(timeout=1)
        if process.is_alive():
            process.kill()

    if status == 'error':
        raise TemplateValidationError([f'Шаблон не отрендерился: {payload}'])
    return payload


def validate_template(
    source: str,
    *,
    allowed: set[str],
    sample: dict[str, Any],
    is_html: bool = True,
    max_bytes: int = 65_536,
    timeout: float = 2.0,
) -> None:
    """Три проверки из теории. Бросает ``TemplateValidationError`` со всеми сразу.

    Ошибки собираются списком, а не по первой: менеджер должен увидеть всё, что
    надо исправить, за одно сохранение формы.
    """
    errors: list[str] = []

    size = len(source.encode('utf-8'))
    if size > max_bytes:
        errors.append(f'Шаблон больше {max_bytes} байт ({size}) — такое письмо всё равно обрежут почтовые клиенты')
        raise TemplateValidationError(errors)

    # 1. Синтаксис.
    env = build_environment(is_html=is_html)
    try:
        ast = env.parse(source)
    except TemplateError as exc:
        raise TemplateValidationError([f'Синтаксическая ошибка: {exc}']) from exc

    # 2. Только доступные возможности.
    used = set(meta.find_undeclared_variables(ast))
    forbidden = used - allowed
    if forbidden:
        stripped = sorted(forbidden & set(STRIPPED_GLOBALS))
        unknown = sorted(forbidden - set(STRIPPED_GLOBALS))
        if unknown:
            errors.append(f'Недоступные переменные: {", ".join(unknown)}. Разрешены: {", ".join(sorted(allowed))}')
        if stripped:
            errors.append(f'Эти возможности шаблонизатора отключены: {", ".join(stripped)}')

    # 3. Письмо рендерится. Недостающие переменные добираем пустыми строками —
    # проверяем сборку, а не полноту примера, иначе ошибка была бы уже поймана
    # проверкой 2.
    context = dict.fromkeys(used, '')
    context.update(sample)
    try:
        render_guarded(source, context, is_html=is_html, timeout=timeout)
    except TemplateValidationError as exc:
        errors.extend(exc.errors)

    if errors:
        raise TemplateValidationError(errors)
