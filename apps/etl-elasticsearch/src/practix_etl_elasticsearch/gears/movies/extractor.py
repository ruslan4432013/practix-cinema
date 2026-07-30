"""Извлечение изменённых фильмов из Postgres.

Общий механизм инкрементального чтения — в ``gears/base/extractor.py``: он был
побайтово одинаков у всех трёх сущностей и различался только ключом состояния и
запросом. Здесь остались ровно эти две вещи.
"""

from practix_etl_elasticsearch.gears.base.extractor import (
    EPOCH_START,
    PG_DSL,
    IncrementalExtractor,
    normalize_datetime,
)

# Реэкспорт для обратной совместимости: раньше genres и persons импортировали эти
# имена ИЗ ЭТОГО модуля (причём normalize_datetime — как приватную _normalize).
__all__ = ['EPOCH_START', 'PG_DSL', 'STATE_KEY_LAST_MODIFIED', 'PostgresExtractor', 'normalize_datetime']

STATE_KEY_LAST_MODIFIED = 'film_work_last_modified'

# Один запрос вместо трёх обращений: персоны и жанры собираются агрегатами
# json_agg с FILTER, поэтому фильм без персон или без жанров всё равно попадает в
# выборку (иначе INNER JOIN тихо отбросил бы его), а COALESCE отдаёт пустой
# массив вместо NULL.
SQL_FETCH_FILMS = """
SELECT
    fw.id,
    fw.title,
    fw.description,
    fw.rating,
    fw.type,
    fw.creation_date,
    fw.created,
    fw.modified,
    COALESCE (
        json_agg(
            DISTINCT jsonb_build_object(
                'person_role', pfw.role,
                'person_id', p.id,
                'person_name', p.full_name
            )
        ) FILTER (WHERE p.id IS NOT NULL),
        '[]'
    ) AS persons,
    COALESCE(
        json_agg(json_build_object('id', g.id::text, 'name', g.name)) FILTER (WHERE g.id IS NOT NULL),
        '[]'::json
    ) AS genres
FROM content.film_work fw
LEFT JOIN content.person_film_work pfw ON pfw.film_work_id = fw.id
LEFT JOIN content.person p ON p.id = pfw.person_id
LEFT JOIN content.genre_film_work gfw ON gfw.film_work_id = fw.id
LEFT JOIN content.genre g ON g.id = gfw.genre_id
WHERE fw.modified > %s
GROUP BY fw.id
ORDER BY fw.modified
LIMIT %s;
"""


class PostgresExtractor(IncrementalExtractor):
    """Извлекает изменённые фильмы из Postgres пачками."""

    state_key = STATE_KEY_LAST_MODIFIED
    sql = SQL_FETCH_FILMS
    entity_name = 'фильмов'
