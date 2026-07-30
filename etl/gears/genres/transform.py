from typing import Any


class GenresTransform:
    """Преобразует строки жанров из Postgres в документы для Elasticsearch."""

    def transform(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        docs: list[dict[str, Any]] = []
        for row in rows:
            doc = {
                'id': str(row['id']),
                'name': row.get('name') or '',
                'description': row.get('description') or '',
            }
            docs.append(doc)
        return docs
