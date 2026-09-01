"""Лестница деградации: раздел 5.2 ТЗ, проверенный по ступеням.

Инвариант, который здесь защищается, один: ни один отказ источника не имеет
права превратиться в 5xx на странице фильма. Источники подменены заглушками —
проверяется решение, а не PostgreSQL с Redis.
"""

import asyncio
import datetime
import uuid

import pytest

from practix_recommendations_api.core.config import settings
from practix_recommendations_api.models.schemas import RecommendationSource
from practix_recommendations_api.services.cache import ShelfCache
from practix_recommendations_api.services.degradation import (
    FilmNotFound,
    LastKnownPopular,
    RecommendationService,
)
from practix_recommendations_api.services.shelf import ShelfState

FILM = uuid.UUID(int=1)
OTHER_FILM = uuid.UUID(int=2)
USER = uuid.UUID(int=100)
NOW = datetime.datetime(2026, 8, 30, 12, 0, tzinfo=datetime.UTC)


class FakeReader:
    """Витрина, которой можно приказать упасть."""

    def __init__(self, *, version=1, similar=None, personal=None, popular=None, catalog=None, broken=False):
        self._state = ShelfState(version=version, finished_at=NOW, models='cooccurrence')
        self._similar = similar or {}
        self._personal = personal or {}
        self._popular = popular or []
        self._catalog = catalog if catalog is not None else {FILM, OTHER_FILM}
        self.broken = broken

    def _guard(self):
        if self.broken:
            raise RuntimeError('витрина недоступна')

    async def get_state(self):
        self._guard()
        return self._state

    async def similar(self, version, film_id, limit):
        self._guard()
        return self._similar.get(film_id, [])[:limit]

    async def personal(self, version, user_id, limit):
        self._guard()
        return self._personal.get(user_id, [])[:limit]

    async def popular(self, version, limit):
        self._guard()
        return self._popular[:limit]

    async def catalog_lookup(self, version, film_id):
        self._guard()
        return film_id in self._catalog, bool(self._catalog)


def build(reader: FakeReader, last_known: LastKnownPopular | None = None) -> RecommendationService:
    # ShelfCache(None) — «Redis не поднят»: ровно вторая ступень лестницы.
    return RecommendationService(reader, ShelfCache(None), last_known or LastKnownPopular())


async def test_similar_returns_neighbours_in_shelf_order():
    reader = FakeReader(similar={FILM: [(OTHER_FILM, 0.9), (uuid.UUID(int=3), 0.4)]})

    result = await build(reader).similar(FILM, limit=10)

    assert result.source is RecommendationSource.SIMILAR
    assert [film for film, _ in result.items] == [OTHER_FILM, uuid.UUID(int=3)]


async def test_known_film_without_neighbours_falls_back_to_popular_not_404():
    """F1.3 и F2.4: блок обязан отрисоваться, а 404 значит «фильма нет»."""
    reader = FakeReader(similar={}, popular=[(OTHER_FILM, 5.0)])

    result = await build(reader).similar(FILM, limit=10)

    assert result.source is RecommendationSource.POPULAR
    assert [film for film, _ in result.items] == [OTHER_FILM]


async def test_unknown_film_is_the_only_404_in_the_service():
    reader = FakeReader(similar={}, popular=[(OTHER_FILM, 5.0)], catalog={OTHER_FILM})

    with pytest.raises(FilmNotFound):
        await build(reader).similar(FILM, limit=10)


async def test_catalog_is_not_consulted_when_neighbours_were_found():
    """Проверка каталога стоит похода в базу, а на счастливом пути не нужна.

    Если у фильма есть соседи, он заведомо существует. Спрашивать заранее
    значило бы платить за 404 на каждом успешном запросе.
    """
    calls: list[uuid.UUID] = []
    reader = FakeReader(similar={FILM: [(OTHER_FILM, 0.9)]})
    original = reader.catalog_lookup

    async def counted(version, film_id):
        calls.append(film_id)
        return await original(version, film_id)

    reader.catalog_lookup = counted

    await build(reader).similar(FILM, limit=10)

    assert calls == []


async def test_shelf_outage_answers_with_popular_instead_of_404():
    """Доказать отсутствие фильма нечем — значит 404 мы не имеем права.

    Выдумывать отсутствие по недоступности источника — худший вид лжи в API:
    фронт снял бы блок и, возможно, показал бы «фильм не найден» на живом фильме.
    """
    reader = FakeReader(similar={})
    last_known = LastKnownPopular()
    last_known.remember(1, [(OTHER_FILM, 3.0)])
    service = build(reader, last_known)

    reader.broken = True
    result = await service.similar(FILM, limit=10)

    assert result.source is RecommendationSource.POPULAR
    assert [film for film, _ in result.items] == [OTHER_FILM]


async def test_completely_empty_shelf_answers_200_with_an_empty_list():
    """Первый запуск: обучение ещё не проходило. Это 200, а не 500 и не 404."""
    reader = FakeReader(version=None, popular=[])

    result = await build(reader).popular(limit=10)

    assert result.source is RecommendationSource.POPULAR
    assert result.items == []
    assert result.version is None


async def test_user_without_history_gets_popular():
    """F4.4: холодный старт пользователя — штатный случай, а не ошибка."""
    reader = FakeReader(personal={}, popular=[(OTHER_FILM, 7.0)])

    result = await build(reader).personal(USER, limit=10)

    assert result.source is RecommendationSource.POPULAR
    assert [film for film, _ in result.items] == [OTHER_FILM]


async def test_personal_outranks_popular_when_the_shelf_has_it():
    reader = FakeReader(personal={USER: [(FILM, 0.8)]}, popular=[(OTHER_FILM, 7.0)])

    result = await build(reader).personal(USER, limit=10)

    assert result.source is RecommendationSource.PERSONAL
    assert [film for film, _ in result.items] == [FILM]


async def test_last_known_popular_survives_a_later_outage():
    """Ступень 3 наполняется успешными чтениями, а не появляется из воздуха."""
    reader = FakeReader(popular=[(OTHER_FILM, 7.0)])
    last_known = LastKnownPopular()
    service = build(reader, last_known)

    await service.popular(limit=10)
    reader.broken = True
    result = await service.popular(limit=10)

    assert [film for film, _ in result.items] == [OTHER_FILM]


async def test_last_known_popular_never_forgets_in_favour_of_nothing():
    """Пустой ответ не имеет права затереть накопленное.

    Иначе первый же промах во время выкладки новой версии стёр бы последнюю
    ступень лестницы ровно тогда, когда она нужнее всего.
    """
    last_known = LastKnownPopular()
    last_known.remember(1, [(FILM, 1.0)])
    last_known.remember(2, [])

    assert last_known.recall() == (1, [(FILM, 1.0)])


async def test_limit_is_respected_on_every_rung():
    reader = FakeReader(
        similar={FILM: [(uuid.UUID(int=index), 1.0) for index in range(10, 30)]},
        popular=[(uuid.UUID(int=index), 1.0) for index in range(30, 50)],
    )
    service = build(reader)

    assert len((await service.similar(FILM, limit=3)).items) == 3
    assert len((await service.popular(limit=2)).items) == 2


async def test_a_silent_source_is_cut_off_by_the_budget(monkeypatch):
    """Отказ бывает не «ошибкой», а «молчанием», и лестница обязана его пережить.

    Витрина, которая не отвечает вовсе (остановленный контейнер, разрыв сети),
    не даёт исключения — она даёт ожидание, и ``except`` ниже не выполняется
    никогда. Без общего срока запрос перекрыл бы таймаут nginx, и пользователь
    получил бы 504 вместо 200 с популярным.
    """
    monkeypatch.setattr(settings, 'RECS_REQUEST_BUDGET', 0.05)

    class SilentReader(FakeReader):
        async def get_state(self):
            await asyncio.sleep(10)

    last_known = LastKnownPopular()
    last_known.remember(1, [(OTHER_FILM, 3.0)])

    result = await build(SilentReader(), last_known).similar(FILM, limit=10)

    assert result.source is RecommendationSource.POPULAR
    assert [film for film, _ in result.items] == [OTHER_FILM]


async def test_the_budget_never_swallows_a_404(monkeypatch):
    """404 — ответ по существу, а не отказ источника.

    Подменять его популярным значило бы врать про состав каталога.
    """
    monkeypatch.setattr(settings, 'RECS_REQUEST_BUDGET', 5.0)
    reader = FakeReader(similar={}, popular=[(OTHER_FILM, 1.0)], catalog={OTHER_FILM})

    with pytest.raises(FilmNotFound):
        await build(reader).similar(FILM, limit=10)
