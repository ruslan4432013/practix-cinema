"""Служебные ручки и обогащение карточек из Movies API."""

import uuid

from practix_recommendations_api.api.v1 import dependencies

FILM = uuid.UUID(int=1)
NEIGHBOUR = uuid.UUID(int=2)


class FakeCatalog:
    """Заглушка Movies API: набор проверяет выдачу, а не каталог."""

    def __init__(self, cards=None, broken: bool = False):
        self.cards = cards or {}
        self.broken = broken
        self.calls = 0

    async def fetch(self, film_ids):
        self.calls += 1
        if self.broken:
            return {}
        return {film_id: self.cards[film_id] for film_id in film_ids if film_id in self.cards}


async def test_liveness_does_not_depend_on_the_shelf(client):
    response = await client.get('/health/live')

    assert response.status_code == 200
    assert response.json() == {'status': 'ok'}


async def test_readiness_reports_an_empty_shelf_as_degraded_but_still_200(client):
    """Пустая витрина — не повод выводить реплику из ротации.

    Соседняя реплика отдаст ровно то же самое: 503 здесь означал бы, что
    трафик некуда деть вовсе.
    """
    response = await client.get('/health/ready')

    assert response.status_code == 200
    body = response.json()
    assert body['status'] == 'degraded'
    assert body['shelf_connected'] is True
    assert body['shelf_version'] is None


async def test_readiness_reports_shelf_age_once_the_shelf_exists(client, shelf):
    """F0.4: возраст витрины обновляет именно проба — горячий путь за ним не ходит."""
    version = await shelf(popular=[(FILM, 1.0)])

    body = (await client.get('/health/ready')).json()

    assert body['status'] == 'ok'
    assert body['shelf_version'] == version
    assert body['shelf_age_seconds'] is not None
    assert body['shelf_age_seconds'] >= 0


async def test_metrics_expose_the_degradation_counters(client, shelf):
    """Деградация обязана быть видна в метриках, а не только в поле source."""
    await shelf(similar={}, popular=[(FILM, 1.0)])
    await client.get(f'/api/v1/recommendations/similar/{FILM}')

    body = (await client.get('/metrics')).text

    assert 'recs_requests_total' in body
    assert 'recs_shelf_age_seconds' in body
    assert 'kind="similar",source="popular"' in body


async def test_enrich_fills_titles_from_the_catalog(client, shelf):
    """E7: стык «блок рекомендаций → каталог» проверяется, а не описывается словами."""
    await shelf(similar={FILM: [(NEIGHBOUR, 0.9)]}, popular=[(FILM, 1.0)])
    dependencies.catalog_client = FakeCatalog({NEIGHBOUR: ('Звёздные войны', 8.6)})
    try:
        body = (await client.get(f'/api/v1/recommendations/similar/{FILM}?enrich=true')).json()
    finally:
        dependencies.catalog_client = None

    assert body['items'][0]['title'] == 'Звёздные войны'
    assert body['items'][0]['imdb_rating'] == 8.6


async def test_enrich_is_off_by_default(client, shelf):
    """Горячий путь отдаёт идентификаторы: каталог дублировать мы не будем (F2.5)."""
    await shelf(similar={FILM: [(NEIGHBOUR, 0.9)]}, popular=[(FILM, 1.0)])
    catalog = FakeCatalog({NEIGHBOUR: ('Звёздные войны', 8.6)})
    dependencies.catalog_client = catalog
    try:
        body = (await client.get(f'/api/v1/recommendations/similar/{FILM}')).json()
    finally:
        dependencies.catalog_client = None

    assert catalog.calls == 0
    assert body['items'][0]['title'] is None


async def test_catalog_outage_degrades_to_bare_identifiers(client, shelf):
    """Недоступность украшения не имеет права уронить блок."""
    await shelf(similar={FILM: [(NEIGHBOUR, 0.9)]}, popular=[(FILM, 1.0)])
    dependencies.catalog_client = FakeCatalog(broken=True)
    try:
        response = await client.get(f'/api/v1/recommendations/similar/{FILM}?enrich=true')
    finally:
        dependencies.catalog_client = None

    assert response.status_code == 200
    assert response.json()['items'][0]['film_id'] == str(NEIGHBOUR)
    assert response.json()['items'][0]['title'] is None


async def test_openapi_lives_on_its_own_prefix(client):
    """/api/openapi занят Movies API, /api/ugc — UGC, /api/shortener — ссылками."""
    assert (await client.get('/api/recommendations/openapi.json')).status_code == 200
