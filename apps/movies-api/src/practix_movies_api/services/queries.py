"""Билдеры тел Elasticsearch-запросов.

SRP: каждый модуль/функция отвечает только за формирование структуры запроса
к ES и ничего не знает о кешировании, моделях или транспорте.
OCP: добавление новых сценариев поиска/фильтрации не требует менять сервисы —
достаточно добавить новый билдер.
"""


def _paginate(body: dict, page_number: int, page_size: int) -> dict:
    body['from'] = (page_number - 1) * page_size
    body['size'] = page_size
    return body


def films_list_query(
    sort: str | None,
    genre: str | None,
    page_number: int,
    page_size: int,
    roles: list[str] | None = None,
) -> dict:
    """Формирует запрос для получения списка фильмов с фильтрацией и сортировкой."""
    roles = roles or []
    is_subscriber = 'subscriber' in roles or 'admin' in roles

    must_queries: list = []
    if genre:
        must_queries.append({'nested': {'path': 'genres', 'query': {'term': {'genres.id': genre}}}})

    # Если пользователь не подписчик, показываем только публичный контент
    if not is_subscriber:
        must_queries.append({'term': {'access_type': 'public'}})

    if not must_queries:
        query = {'match_all': {}}
    elif len(must_queries) == 1:
        query = must_queries[0]
    else:
        query = {'bool': {'must': must_queries}}

    sort_field = 'imdb_rating'
    sort_order = 'desc'
    if sort:
        if sort.startswith('-'):
            sort_field = sort[1:]
            sort_order = 'desc'
        else:
            sort_field = sort
            sort_order = 'asc'

    return _paginate(
        {'query': query, 'sort': [{sort_field: {'order': sort_order}}]},
        page_number,
        page_size,
    )


def films_search_query(query: str, page_number: int, page_size: int, roles: list[str] | None = None) -> dict:
    """Формирует запрос для полнотекстового поиска фильмов."""
    roles = roles or []
    is_subscriber = 'subscriber' in roles or 'admin' in roles

    main_query = {'multi_match': {'query': query, 'fields': ['title', 'description']}}

    # Если пользователь не подписчик, ограничиваем поиск публичным контентом
    if not is_subscriber:
        return _paginate(
            {'query': {'bool': {'must': [main_query, {'term': {'access_type': 'public'}}]}}},
            page_number,
            page_size,
        )

    return _paginate(
        {'query': main_query},
        page_number,
        page_size,
    )


def persons_search_query(query: str, page_number: int, page_size: int) -> dict:
    """Запрос для полнотекстового поиска персон."""
    return _paginate(
        {'query': {'multi_match': {'query': query, 'fields': ['full_name']}}},
        page_number,
        page_size,
    )


def persons_list_query(page_number: int, page_size: int) -> dict:
    """Запрос для получения списка всех персон."""
    return _paginate({'query': {'match_all': {}}}, page_number, page_size)


def _person_films_bool(person_id: str) -> dict:
    """Вспомогательный метод для фильтрации фильмов по персоне."""
    return {
        'bool': {
            'should': [
                {'nested': {'path': 'actors', 'query': {'term': {'actors.id': person_id}}}},
                {'nested': {'path': 'directors', 'query': {'term': {'directors.id': person_id}}}},
                {'nested': {'path': 'writers', 'query': {'term': {'writers.id': person_id}}}},
            ],
            'minimum_should_match': 1,
        }
    }


def person_films_roles_query(person_id: str) -> dict:
    """Запрос для получения ролей персоны в фильмах."""
    return {
        'query': _person_films_bool(person_id),
        'size': 1000,
        '_source': ['id', 'actors', 'directors', 'writers'],
    }


def person_films_short_query(person_id: str) -> dict:
    """Запрос для получения списка фильмов персоны."""
    return {
        'query': _person_films_bool(person_id),
        'size': 1000,
        '_source': ['id', 'title', 'imdb_rating'],
    }


def genres_all_query() -> dict:
    """Запрос для получения всех жанров."""
    return {'query': {'match_all': {}}, 'size': 1000}
