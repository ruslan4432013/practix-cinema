"""Валидация шаблонов: три проверки теории и границы песочницы."""

import pytest

from practix_notifications.services.templating import (
    STRIPPED_GLOBALS,
    TemplateValidationError,
    find_variables,
    render,
    validate_template,
)

ALLOWED = {'login', 'film_title'}
SAMPLE = {'login': 'ivan', 'film_title': 'Звёздные войны'}


def test_valid_template_passes():
    validate_template('Привет, {{ login }}!', allowed=ALLOWED, sample=SAMPLE)


def test_syntax_error_rejected():
    with pytest.raises(TemplateValidationError) as exc:
        validate_template('Привет, {% if login %}', allowed=ALLOWED, sample=SAMPLE)
    assert 'Синтаксическая ошибка' in exc.value.errors[0]


def test_variable_outside_whitelist_rejected():
    """Проверка №2: менеджеру доступен только белый список переменных."""
    with pytest.raises(TemplateValidationError) as exc:
        validate_template('{{ login }} {{ secret_token }}', allowed=ALLOWED, sample=SAMPLE)
    assert any('secret_token' in error for error in exc.value.errors)


def test_stripped_globals_are_not_available():
    """Без range в шаблоне нечего итерировать — «вечный цикл» недостижим."""
    with pytest.raises(TemplateValidationError) as exc:
        validate_template('{% for i in range(10) %}{{ i }}{% endfor %}', allowed=ALLOWED, sample=SAMPLE)
    assert any('отключены' in error for error in exc.value.errors)
    assert 'range' in STRIPPED_GLOBALS


def test_sandbox_blocks_attribute_access():
    """Песочница закрывает доступ к внутренностям объектов."""
    with pytest.raises(TemplateValidationError):
        validate_template('{{ login.__class__ }}', allowed=ALLOWED, sample=SAMPLE)


def test_oversized_template_rejected():
    with pytest.raises(TemplateValidationError) as exc:
        validate_template('x' * 100, allowed=ALLOWED, sample=SAMPLE, max_bytes=10)
    assert 'больше 10 байт' in exc.value.errors[0]


def test_render_timeout_kills_runaway_template():
    """Поточный таймаут не прервал бы зациклившийся рендер — нужен процесс."""
    # Цикл строится не через range (его нет), а через фильтр, раскручивающий
    # строку до огромного размера: рендер уходит в себя надолго.
    source = "{{ ('a' * 10000000) * 200 }}"
    with pytest.raises(TemplateValidationError) as exc:
        validate_template(source, allowed=set(), sample={}, timeout=0.3)
    assert exc.value.errors


def test_autoescape_depends_on_format():
    payload = {'login': '<b>ivan</b>'}
    assert render('{{ login }}', payload, is_html=True) == '&lt;b&gt;ivan&lt;/b&gt;'
    # В текстовом письме экранирование превратило бы теги в мусор на экране.
    assert render('{{ login }}', payload, is_html=False) == '<b>ivan</b>'


def test_find_variables_reports_externals():
    assert find_variables('{{ a }}{% if b %}{{ c }}{% endif %}') == {'a', 'b', 'c'}
