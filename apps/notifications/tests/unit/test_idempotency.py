"""Ключ идемпотентности: стабилен и различает то, что должен различать."""

from practix_notifications.services.idempotency import KEY_LENGTH, delivery_key


def test_key_is_deterministic():
    first = delivery_key('run-1', 'sub-1', 'email')
    second = delivery_key('run-1', 'sub-1', 'email')
    assert first == second


def test_format_is_frozen():
    """Смена алгоритма задним числом сделала бы все сохранённые задачи новыми —
    и повторный запуск разослал бы всё заново."""
    assert delivery_key('run-1', 'sub-1', 'email') == 'f6011312f4ebf1cef08ae4785ccf1edf'
    assert len(delivery_key('run-1', 'sub-1', 'email')) == KEY_LENGTH


def test_different_run_gives_different_key():
    """Повторяющаяся рассылка обязана дойти до того же человека и в следующий раз."""
    assert delivery_key('run-1', 'sub-1', 'email') != delivery_key('run-2', 'sub-1', 'email')


def test_different_subscriber_gives_different_key():
    assert delivery_key('run-1', 'sub-1', 'email') != delivery_key('run-1', 'sub-2', 'email')


def test_different_channel_gives_different_key():
    """Одно и то же уведомление в почту и в push — две разные доставки."""
    assert delivery_key('run-1', 'sub-1', 'email') != delivery_key('run-1', 'sub-1', 'push')


def test_separator_is_not_ambiguous():
    """Склейка без разделителя дала бы одинаковый ключ разным парам."""
    assert delivery_key('a', 'bc', 'email') != delivery_key('ab', 'c', 'email')
