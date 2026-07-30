import datetime
from typing import Any


class DataTransform:
    """Преобразует строки из Postgres в документы для Elasticsearch."""

    @staticmethod
    def _split_persons(persons: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
        """Распределяет участников фильма по ролям (режиссер, актер, сценарист)."""
        directors, actors, writers = [], [], []
        seen = {'director': set(), 'actor': set(), 'writer': set()}
        for p in persons:
            role = p.get('person_role')
            pid = p.get('person_id')
            name = p.get('person_name')
            if not pid or not name or role not in seen:
                continue
            if pid in seen[role]:
                continue
            seen[role].add(pid)
            entry = {'id': pid, 'name': name}
            if role == 'director':
                directors.append(entry)
            elif role == 'actor':
                actors.append(entry)
            elif role == 'writer':
                writers.append(entry)
        return directors, actors, writers

    def transform(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Основной метод трансформации данных."""
        docs: list[dict[str, Any]] = []
        for row in rows:
            directors, actors, writers = self._split_persons(row.get('persons') or [])
            # Дедупликация жанров по id
            raw_genres = row.get('genres') or []
            seen: dict = {}
            for g in raw_genres:
                if g and g.get('id') and g['id'] not in seen:
                    seen[g['id']] = g
            doc = {
                'id': str(row['id']),
                'imdb_rating': float(row['rating']) if row['rating'] is not None else None,
                'genres': list(seen.values()),
                'title': row.get('title') or '',
                'description': row.get('description') or '',
                'directors_names': [d['name'] for d in directors],
                'actors_names': [a['name'] for a in actors],
                'writers_names': [w['name'] for w in writers],
                'directors': directors,
                'actors': actors,
                'writers': writers,
            }

            # Определение типа доступа: новинки (последние 3 года) только для подписчиков
            three_years_ago = datetime.date.today() - datetime.timedelta(days=3 * 365)
            creation_date = row.get('creation_date')
            if creation_date and creation_date >= three_years_ago:
                doc['access_type'] = 'subscribers'
            else:
                doc['access_type'] = 'public'

            docs.append(doc)
        return docs
