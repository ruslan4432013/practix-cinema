"""Схема индекса Elasticsearch для сущности «movies» — реэкспорт.

Определение переехало в ``practix_search_schema``: та же схема побайтово
дублировалась в тестовой фикстуре, из-за чего набор создавал индекс по своей
копии и проверял сам себя — расхождение с продом не поймал бы никто.
"""

from practix_search_schema import MOVIES_INDEX as INDEX_BODY

__all__ = ['INDEX_BODY']
