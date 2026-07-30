"""Извлечение изменённых персон из Postgres.

Общий механизм — в ``gears/base/extractor.py``.
"""

from practix_etl_elasticsearch.gears.base.extractor import IncrementalExtractor

STATE_KEY_PERSONS_MODIFIED = 'person_last_modified'

SQL_FETCH_PERSONS = """
SELECT
    p.id,
    p.full_name,
    p.modified
FROM content.person p
WHERE p.modified > %s
ORDER BY p.modified
LIMIT %s;
"""


class PersonsExtractor(IncrementalExtractor):
    """Извлекает изменённых персон из Postgres пачками."""

    state_key = STATE_KEY_PERSONS_MODIFIED
    sql = SQL_FETCH_PERSONS
    entity_name = 'персон'
