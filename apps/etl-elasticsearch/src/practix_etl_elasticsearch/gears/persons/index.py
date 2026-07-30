"""Схема индекса Elasticsearch для сущности «persons» — реэкспорт.

Определение переехало в ``practix_search_schema``: та же схема побайтово
дублировалась в тестовой фикстуре, из-за чего набор создавал индекс по своей
копии и проверял сам себя — расхождение с продом не поймал бы никто.
"""

from practix_search_schema import PERSONS_INDEX as PERSONS_INDEX_BODY

__all__ = ['PERSONS_INDEX_BODY']
