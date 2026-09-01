"""Сквозные проверки выдачи против живых recs-db и redis-recs.

Главный инвариант набора — раздел 5.2 ТЗ: ни один отказ источника не имеет
права превратиться в 5xx на странице фильма. Поэтому здесь гасятся настоящие
Redis и PostgreSQL, а не подменяются заглушками.
"""

import uuid

from practix_testing.utils.helpers import auth_header

FILM = uuid.UUID(int=1)
NEIGHBOUR = uuid.UUID(int=2)
FAR_FILM = uuid.UUID(int=3)
POPULAR_FILM = uuid.UUID(int=9)
UNKNOWN_FILM = uuid.UUID(int=999)
USER = uuid.uuid4()


async def test_similar_returns_neighbours_in_order(client, shelf):
    version = await shelf(
        similar={FILM: [(NEIGHBOUR, 0.9), (FAR_FILM, 0.3)]},
        popular=[(POPULAR_FILM, 100.0)],
    )

    response = await client.get(f'/api/v1/recommendations/similar/{FILM}')

    assert response.status_code == 200
    body = response.json()
    assert body['source'] == 'similar'
    assert body['version'] == version
    assert [item['film_id'] for item in body['items']] == [str(NEIGHBOUR), str(FAR_FILM)]


async def test_source_film_is_absent_from_its_own_block(client, shelf):
    await shelf(similar={FILM: [(NEIGHBOUR, 0.9)]}, popular=[(POPULAR_FILM, 1.0)])

    body = (await client.get(f'/api/v1/recommendations/similar/{FILM}')).json()

    assert str(FILM) not in [item['film_id'] for item in body['items']]


async def test_known_film_without_neighbours_gets_popular_with_200(client, shelf):
    """F1.3 и F2.4: блок обязан отрисоваться, а источник — быть назван честно."""
    await shelf(similar={}, popular=[(POPULAR_FILM, 100.0)])

    response = await client.get(f'/api/v1/recommendations/similar/{FILM}')

    assert response.status_code == 200
    assert response.json()['source'] == 'popular'
    assert [item['film_id'] for item in response.json()['items']] == [str(POPULAR_FILM)]


async def test_unknown_film_is_404(client, shelf):
    await shelf(similar={}, popular=[(POPULAR_FILM, 1.0)])

    response = await client.get(f'/api/v1/recommendations/similar/{UNKNOWN_FILM}')

    assert response.status_code == 404


async def test_a_stale_pointer_does_not_turn_the_whole_catalog_into_404s(client, shelf, redis_client):
    """404 отдаётся, только когда отсутствие фильма ДОКАЗАНО.

    Указатель в кэше может пережить свою версию. В несуществующей версии нет ни
    одного фильма по построению, поэтому наивная проверка каталога превратила бы
    в 404 весь каталог разом — на живых фильмах, при исправной базе.
    """
    await shelf(similar={FILM: [(NEIGHBOUR, 0.9)]}, popular=[(POPULAR_FILM, 1.0)])
    await redis_client.set('recs:pointer', '999999')

    response = await client.get(f'/api/v1/recommendations/similar/{FILM}')

    assert response.status_code == 200


async def test_popular_needs_no_token(client, shelf):
    """F1.2: блок для анонимного посетителя."""
    await shelf(popular=[(POPULAR_FILM, 100.0), (NEIGHBOUR, 50.0)])

    response = await client.get('/api/v1/recommendations/popular')

    assert response.status_code == 200
    assert [item['film_id'] for item in response.json()['items']] == [str(POPULAR_FILM), str(NEIGHBOUR)]


async def test_empty_shelf_answers_200_with_an_empty_list(client):
    """Первый запуск: обучения ещё не было. Это не ошибка."""
    response = await client.get('/api/v1/recommendations/popular')

    assert response.status_code == 200
    assert response.json() == {'source': 'popular', 'version': None, 'items': []}


async def test_similar_on_an_empty_shelf_never_404s(client):
    """Состав каталога неизвестен — значит доказать отсутствие фильма нечем."""
    response = await client.get(f'/api/v1/recommendations/similar/{UNKNOWN_FILM}')

    assert response.status_code == 200
    assert response.json()['source'] == 'popular'


async def test_personal_requires_a_token(client, shelf):
    """F4.2: user_id берётся только из токена, параметром его не передать."""
    await shelf(popular=[(POPULAR_FILM, 1.0)])

    assert (await client.get('/api/v1/recommendations/me')).status_code == 401


async def test_personal_ignores_any_attempt_to_pass_a_user_id(client, shelf):
    """Параметра нет — значит и подмены быть не может.

    Если бы он был, выдача стала бы оракулом: по ней восстанавливается история
    просмотров чужого человека.
    """
    await shelf(personal={USER: [(NEIGHBOUR, 0.8)]}, popular=[(POPULAR_FILM, 1.0)])
    victim = uuid.uuid4()

    body = (
        await client.get(
            f'/api/v1/recommendations/me?user_id={USER}',
            headers=auth_header(roles=['user'], user_id=str(victim)),
        )
    ).json()

    # Токен принадлежит victim, у которого истории нет: отдаём популярное,
    # а не выдачу USER, чей идентификатор передан параметром.
    assert body['source'] == 'popular'


async def test_personal_returns_the_shelf_for_the_token_subject(client, shelf):
    await shelf(personal={USER: [(NEIGHBOUR, 0.8), (FAR_FILM, 0.5)]}, popular=[(POPULAR_FILM, 1.0)])

    body = (
        await client.get('/api/v1/recommendations/me', headers=auth_header(roles=['user'], user_id=str(USER)))
    ).json()

    assert body['source'] == 'personal'
    assert [item['film_id'] for item in body['items']] == [str(NEIGHBOUR), str(FAR_FILM)]


async def test_user_without_history_gets_popular(client, shelf):
    """F4.4: холодный старт пользователя."""
    await shelf(personal={}, popular=[(POPULAR_FILM, 1.0)])

    body = (
        await client.get('/api/v1/recommendations/me', headers=auth_header(roles=['user'], user_id=str(uuid.uuid4())))
    ).json()

    assert body['source'] == 'popular'


async def test_limit_is_clamped_to_the_ceiling(client, shelf):
    await shelf(popular=[(uuid.UUID(int=index), float(index)) for index in range(100, 200)])

    body = (await client.get('/api/v1/recommendations/popular?limit=9999')).json()

    assert len(body['items']) == 50


async def test_second_request_is_served_from_the_hot_layer(client, shelf, redis_client):
    """ADR-004: версия батча входит в ключ, и по нему видно, что кэш заполнен."""
    version = await shelf(similar={FILM: [(NEIGHBOUR, 0.9)]}, popular=[(POPULAR_FILM, 1.0)])

    await client.get(f'/api/v1/recommendations/similar/{FILM}')

    assert await redis_client.get(f'recs:v{version}:similar:{FILM}') is not None


async def test_cache_outage_still_answers_from_postgresql(client, shelf, redis_client):
    """Вторая ступень лестницы: медленнее, но это ответ."""
    await shelf(similar={FILM: [(NEIGHBOUR, 0.9)]}, popular=[(POPULAR_FILM, 1.0)])
    await redis_client.aclose()

    response = await client.get(f'/api/v1/recommendations/similar/{FILM}')

    assert response.status_code == 200
    assert response.json()['source'] == 'similar'


async def test_stale_pointer_in_cache_does_not_leak_another_versions_rows(client, shelf, redis_client):
    """F0.2: выдача читает целостную версию, а не смесь старой и новой.

    В кэш кладётся указатель на несуществующую версию — так выглядит окно между
    переключением указателя и истечением ключей. Строк этой версии нет нигде, и
    правильный ответ — пустая выдача, а НЕ строки соседней версии.

    ``version`` при этом null, и это не небрежность: поле описывает, откуда
    пришли отданные позиции, а не то, что показывал указатель. Позиций нет —
    значит и версии у них нет.
    """
    await shelf(similar={FILM: [(NEIGHBOUR, 0.9)]}, popular=[(POPULAR_FILM, 1.0)])
    await redis_client.set('recs:pointer', '999999')

    response = await client.get(f'/api/v1/recommendations/similar/{FILM}')

    assert response.status_code == 200
    body = response.json()
    assert body['items'] == []
    assert body['source'] == 'popular'
    assert body['version'] is None
