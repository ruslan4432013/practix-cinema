"""Схемы индексов Elasticsearch — ЕДИНСТВЕННЫЙ источник истины.

До вынесения каждая схема существовала в ДВУХ побайтово равных копиях: в
``gears/<entity>/index.py`` у ETL (который индексы СОЗДАЁТ в продакшене) и в
``tests/functional/testdata/es_mapping.py`` (откуда их создавал тестовый набор).
Проверено: все три словаря совпадали полностью.

ПОЧЕМУ ЭТОТ ДУБЛЬ БЫЛ ОПАСНЕЕ ОСТАЛЬНЫХ. Тест создавал индекс по СВОЕЙ копии
маппинга и по ней же проверял поиск. Измени кто-нибудь анализатор или тип поля в
продовой схеме — набор остался бы зелёным, потому что проверял бы сам себя.
Расхождение обнаружилось бы уже на боевом кластере как «поиск перестал находить»,
причём без единого падения в CI.

Этот кластер НЕ был в исходном плане переезда — его нашёл jscpd (5 клонов по
26–42 строки, крупнейшая группа после вынесения logger/tracing).
"""

MOVIES_INDEX = {
    'settings': {
        'refresh_interval': '1s',
        'analysis': {
            'filter': {
                'english_stop': {'type': 'stop', 'stopwords': '_english_'},
                'english_stemmer': {'type': 'stemmer', 'language': 'english'},
                'english_possessive_stemmer': {
                    'type': 'stemmer',
                    'language': 'possessive_english',
                },
                'russian_stop': {'type': 'stop', 'stopwords': '_russian_'},
                'russian_stemmer': {'type': 'stemmer', 'language': 'russian'},
            },
            'analyzer': {
                'ru_en': {
                    'tokenizer': 'standard',
                    'filter': [
                        'lowercase',
                        'english_stop',
                        'english_stemmer',
                        'english_possessive_stemmer',
                        'russian_stop',
                        'russian_stemmer',
                    ],
                }
            },
        },
    },
    'mappings': {
        'dynamic': 'strict',
        'properties': {
            'id': {'type': 'keyword'},
            'imdb_rating': {'type': 'float'},
            'access_type': {'type': 'keyword'},
            'genres': {
                'type': 'nested',
                'dynamic': 'strict',
                'properties': {
                    'id': {'type': 'keyword'},
                    'name': {'type': 'text', 'analyzer': 'ru_en'},
                },
            },
            'title': {
                'type': 'text',
                'analyzer': 'ru_en',
                'fields': {'raw': {'type': 'keyword'}},
            },
            'description': {'type': 'text', 'analyzer': 'ru_en'},
            'directors_names': {'type': 'text', 'analyzer': 'ru_en'},
            'actors_names': {'type': 'text', 'analyzer': 'ru_en'},
            'writers_names': {'type': 'text', 'analyzer': 'ru_en'},
            'directors': {
                'type': 'nested',
                'dynamic': 'strict',
                'properties': {
                    'id': {'type': 'keyword'},
                    'name': {'type': 'text', 'analyzer': 'ru_en'},
                },
            },
            'actors': {
                'type': 'nested',
                'dynamic': 'strict',
                'properties': {
                    'id': {'type': 'keyword'},
                    'name': {'type': 'text', 'analyzer': 'ru_en'},
                },
            },
            'writers': {
                'type': 'nested',
                'dynamic': 'strict',
                'properties': {
                    'id': {'type': 'keyword'},
                    'name': {'type': 'text', 'analyzer': 'ru_en'},
                },
            },
        },
    },
}

GENRES_INDEX = {
    'settings': {
        'refresh_interval': '1s',
        'analysis': {
            'filter': {
                'english_stop': {'type': 'stop', 'stopwords': '_english_'},
                'english_stemmer': {'type': 'stemmer', 'language': 'english'},
                'english_possessive_stemmer': {
                    'type': 'stemmer',
                    'language': 'possessive_english',
                },
                'russian_stop': {'type': 'stop', 'stopwords': '_russian_'},
                'russian_stemmer': {'type': 'stemmer', 'language': 'russian'},
            },
            'analyzer': {
                'ru_en': {
                    'tokenizer': 'standard',
                    'filter': [
                        'lowercase',
                        'english_stop',
                        'english_stemmer',
                        'english_possessive_stemmer',
                        'russian_stop',
                        'russian_stemmer',
                    ],
                }
            },
        },
    },
    'mappings': {
        'dynamic': 'strict',
        'properties': {
            'id': {'type': 'keyword'},
            'name': {
                'type': 'text',
                'analyzer': 'ru_en',
                'fields': {'raw': {'type': 'keyword'}},
            },
            'description': {'type': 'text', 'analyzer': 'ru_en'},
        },
    },
}

PERSONS_INDEX = {
    'settings': {
        'refresh_interval': '1s',
        'analysis': {
            'filter': {
                'english_stop': {'type': 'stop', 'stopwords': '_english_'},
                'english_stemmer': {'type': 'stemmer', 'language': 'english'},
                'english_possessive_stemmer': {
                    'type': 'stemmer',
                    'language': 'possessive_english',
                },
                'russian_stop': {'type': 'stop', 'stopwords': '_russian_'},
                'russian_stemmer': {'type': 'stemmer', 'language': 'russian'},
            },
            'analyzer': {
                'ru_en': {
                    'tokenizer': 'standard',
                    'filter': [
                        'lowercase',
                        'english_stop',
                        'english_stemmer',
                        'english_possessive_stemmer',
                        'russian_stop',
                        'russian_stemmer',
                    ],
                }
            },
        },
    },
    'mappings': {
        'dynamic': 'strict',
        'properties': {
            'id': {'type': 'keyword'},
            'full_name': {
                'type': 'text',
                'analyzer': 'ru_en',
                'fields': {'raw': {'type': 'keyword'}},
            },
        },
    },
}


# Имя индекса -> схема. Удобно для скриптов и для теста, проверяющего, что
# продовые и тестовые определения не разъехались.
INDEX_SCHEMAS = {
    'movies': MOVIES_INDEX,
    'genres': GENRES_INDEX,
    'persons': PERSONS_INDEX,
}
