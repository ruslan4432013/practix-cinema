"""Генерация коротких кодов."""

import string

from practix_link_shortener.services import codes


def test_alphabet_is_base62_without_separators():
    # Ни '-', ни '_': код диктуют голосом и набирают руками, а дефис в конце
    # строки почтовые клиенты любят превращать в перенос.
    assert set(codes.ALPHABET) == set(string.digits + string.ascii_letters)
    assert len(codes.ALPHABET) == 62


def test_length_is_honoured():
    for length in (6, 7, 12, 16):
        assert len(codes.make_code(length)) == length


def test_default_length_is_seven():
    assert len(codes.make_code()) == codes.DEFAULT_LENGTH == 7


def test_code_uses_only_alphabet_characters():
    allowed = set(codes.ALPHABET)
    for _ in range(200):
        assert set(codes.make_code()) <= allowed


def test_codes_are_not_predictable_from_each_other():
    """Не строгая проверка энтропии, а сторож против случайного возврата к счётчику.

    Перечислимые коды — прямая дыра: ссылка ПОДТВЕРЖДАЕТ адрес электронной
    почты, и перебор соседних кодов подтвердил бы чужой ящик.
    """
    generated = [codes.make_code() for _ in range(500)]
    assert len(set(generated)) == len(generated)


def test_source_of_randomness_is_secrets(monkeypatch):
    """``secrets``, а не ``random``: состояние Mersenne Twister восстановимо."""
    called = []
    real_choice = codes.secrets.choice

    def spy(seq):
        called.append(seq)
        return real_choice(seq)

    monkeypatch.setattr(codes.secrets, 'choice', spy)
    codes.make_code(7)
    assert len(called) == 7
