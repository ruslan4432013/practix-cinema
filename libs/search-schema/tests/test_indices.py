"""Проверки схем индексов.

Схемы — это данные, поэтому тесты проверяют не поведение, а инварианты, из-за
нарушения которых поиск молча перестаёт находить: наличие анализаторов, типы
полей и совпадение набора индексов с тем, что создаёт ETL.
"""

from practix_search_schema import (
    GENRES_INDEX,
    INDEX_SCHEMAS,
    MOVIES_INDEX,
    PERSONS_INDEX,
)


def test_all_three_indices_are_exported():
    assert set(INDEX_SCHEMAS) == {'movies', 'genres', 'persons'}
    assert INDEX_SCHEMAS['movies'] is MOVIES_INDEX
    assert INDEX_SCHEMAS['genres'] is GENRES_INDEX
    assert INDEX_SCHEMAS['persons'] is PERSONS_INDEX


def test_every_index_declares_settings_and_mappings():
    for name, schema in INDEX_SCHEMAS.items():
        assert 'settings' in schema, name
        assert 'mappings' in schema, name
        assert 'properties' in schema['mappings'], name


def test_movies_index_keeps_search_fields():
    """Поля, по которым идёт полнотекстовый поиск, должны остаться text."""
    props = MOVIES_INDEX['mappings']['properties']
    assert props['title']['type'] == 'text'
    assert props['description']['type'] == 'text'
    # imdb_rating сортируется и фильтруется — обязан быть числом, не строкой.
    assert props['imdb_rating']['type'] == 'float'


def test_movies_title_has_raw_subfield_for_sorting():
    """Сортировка по title невозможна по analyzed-полю — нужен keyword-подполе."""
    assert MOVIES_INDEX['mappings']['properties']['title']['fields']['raw']['type'] == 'keyword'


def test_person_and_genre_names_are_searchable():
    assert PERSONS_INDEX['mappings']['properties']['full_name']['type'] == 'text'
    assert GENRES_INDEX['mappings']['properties']['name']['type'] == 'text'


def test_analysis_settings_present_where_used():
    """Кастомный анализатор объявляется в settings.analysis — иначе ES отвергнет маппинг."""
    for name in ('movies', 'genres', 'persons'):
        settings = INDEX_SCHEMAS[name]['settings']
        assert 'analysis' in settings, name
        assert 'analyzer' in settings['analysis'], name
