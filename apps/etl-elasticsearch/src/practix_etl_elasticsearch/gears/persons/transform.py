from typing import Any


class PersonsTransform:
    """Преобразует строки персон из Postgres в документы для Elasticsearch."""

    def transform(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        docs: list[dict[str, Any]] = []
        for row in rows:
            doc = {
                'id': str(row['id']),
                'full_name': row.get('full_name') or '',
            }
            docs.append(doc)
        return docs
