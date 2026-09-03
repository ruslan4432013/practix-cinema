"""Прогрев горячего слоя: списки и указатель применяются вместе.

Главное здесь — не «что записалось», а «что НЕ записалось при сбое». Пока
указатель переставлялся отдельной командой после раскладки списков, ошибка
между двумя действиями оставляла новые данные в Redis, а указатель — на старой
версии; выдача верит указателю раньше, чем базе, и молча отдавала вчерашние
рекомендации до следующего успешного прогрева. Транзакция закрывает это окно,
а снятие устаревшего указателя закрывает то, что транзакция закрыть не может:
полный отказ ``EXEC``, после которого указатель всё равно остался бы старым.
"""

import json

import pytest

from practix_recsys_trainer.core.config import settings
from practix_recsys_trainer.shelf import warmup
from practix_recsys_trainer.shelf.warmup import POINTER_KEY

POPULAR = [('film-a', 9.0), ('film-b', 8.0)]
SIMILAR = {f'film-{index}': [(f'neighbour-{index}', 1.0)] for index in range(5)}


class FakePipeline:
    """``MULTI``/``EXEC``: команды копятся и применяются только в ``execute``."""

    def __init__(self, store: dict[str, tuple[str, int | None]], *, fail: str | None) -> None:
        self._store = store
        self._fail = fail
        self.queued: list[tuple[str, str, int | None]] = []

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.queued.append((key, value, ex))

    def execute(self) -> list[bool]:
        if self._fail == 'before':
            raise ConnectionError('redis ушёл до EXEC')
        for key, value, ex in self.queued:
            self._store[key] = (value, ex)
        if self._fail == 'after':
            # Транзакция применилась на сервере, а ответ до клиента не дошёл.
            raise ConnectionError('ответ на EXEC потерян')
        return [True] * len(self.queued)


class FakeRedis:
    """У клиента намеренно НЕТ ``set``: запись мимо транзакции должна падать."""

    def __init__(self, store: dict[str, tuple[str, int | None]], *, fail: str | None, brittle: bool = False) -> None:
        self.store = store
        self._fail = fail
        self._brittle = brittle
        self.closed = False

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        assert transaction, 'указатель обязан ехать в MULTI/EXEC, а не в пакете команд'
        return FakePipeline(self.store, fail=self._fail)

    def get(self, key: str) -> str | None:
        if self._brittle:
            raise ConnectionError('redis не отвечает и на компенсацию')
        entry = self.store.get(key)
        return entry[0] if entry else None

    def delete(self, key: str) -> int:
        return int(self.store.pop(key, None) is not None)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def client(monkeypatch):
    """Подменяет конструктор клиента, чтобы не менять сигнатуру ``warm``."""

    def make(store=None, *, fail=None, brittle=False) -> FakeRedis:
        fake = FakeRedis(store if store is not None else {}, fail=fail, brittle=brittle)
        monkeypatch.setattr(warmup.redis, 'Redis', lambda **kwargs: fake)
        return fake

    return make


def test_pointer_version_reads_the_hot_layer_not_the_run(client):
    """Гейдж «прогрето» обязан отвечать про Redis, а не про последний прогон.

    Иначе он показывал бы «прогрето» ещё сутки после того, как горячий слой
    опустел мимо трейнера, — и был бы ровно той метрикой, которая зелёная при
    сломанной системе.
    """
    client({POINTER_KEY: ('7', None)})

    assert warmup.pointer_version() == 7


def test_pointer_version_is_none_when_there_is_nothing_to_read(client):
    """Пустой указатель и молчащий Redis — одно и то же: слой не обслуживает версию."""
    client({})
    assert warmup.pointer_version() is None

    client({POINTER_KEY: ('7', None)}, brittle=True)
    assert warmup.pointer_version() is None

    # Мусор вместо номера версии тоже не должен ронять цикл обучения: метрика
    # снимается в том же тике, что и возраст витрины.
    client({POINTER_KEY: ('не число', None)})
    assert warmup.pointer_version() is None


def test_lists_and_pointer_land_together(client):
    fake = client()

    assert warmup.warm(7, popular=POPULAR, similar=SIMILAR) is True

    value, ex = fake.store['recs:v7:popular']
    assert json.loads(value) == [['film-a', 9.0], ['film-b', 8.0]]
    assert ex == settings.RECS_CACHE_TTL
    # Указатель переживает любой список, поэтому без TTL.
    assert fake.store[POINTER_KEY] == ('7', None)
    assert fake.closed


def test_warmup_stops_at_the_configured_number_of_films(client, monkeypatch):
    monkeypatch.setattr(settings, 'RECS_TRAINER_WARMUP_FILMS', 2)
    fake = client()

    warmup.warm(7, popular=POPULAR, similar=SIMILAR)

    assert len([key for key in fake.store if key.startswith('recs:v7:similar:')]) == 2


def test_a_failed_transaction_writes_nothing_and_takes_the_stale_pointer_down(client):
    """Тот самый баг: новые данные без указателя — или указатель без данных."""
    fake = client({POINTER_KEY: ('6', None)}, fail='before')

    assert warmup.warm(7, popular=POPULAR, similar=SIMILAR) is False

    assert 'recs:v7:popular' not in fake.store
    # Указатель на версию 6 хуже отсутствующего: выдача взяла бы вчерашние
    # списки, а в PostgreSQL уже опубликована седьмая.
    assert POINTER_KEY not in fake.store


def test_a_lost_answer_does_not_cost_the_pointer(client):
    """``EXEC`` применился, ответ потерян — указатель уже правильный, не трогаем."""
    fake = client({POINTER_KEY: ('6', None)}, fail='after')

    assert warmup.warm(7, popular=POPULAR, similar=SIMILAR) is False

    assert fake.store[POINTER_KEY] == ('7', None)
    assert 'recs:v7:popular' in fake.store
    # И это тот случай, ради которого счётчик неудач и гейдж состояния — разные
    # метрики: прогрев считает себя неудавшимся, а горячий слой обслуживает
    # актуальную версию. Поэтому алерт на счётчик отсылает смотреть на гейдж.
    assert warmup.pointer_version() == 7


def test_a_failed_compensation_still_does_not_break_the_run(client):
    """Прогрев не имеет права уронить обучение — даже когда не удаётся и уборка."""
    fake = client({POINTER_KEY: ('6', None)}, fail='before', brittle=True)

    assert warmup.warm(7, popular=POPULAR, similar=SIMILAR) is False
    assert fake.closed
