"""Каталог: список идентификаторов фильмов из theatre-db.

Нужен ровно для трёх вещей, и все три — требования ТЗ:

* отличить «фильма нет» (404 по F2.4) от «фильм есть, соседей нет» (200 с
  популярным по F1.3) — для этого снимок каталога кладётся в витрину;
* посчитать покрытие каталога (SLO ≥ 60% фильмов имеют непустой блок);
* дать генератору синтетики НАСТОЯЩИЕ идентификаторы. Это не мелочь: сгенерируй
  просмотры по выдуманным UUID — и рекомендации будут ссылаться на фильмы,
  которых нет в каталоге, а весь сквозной путь окажется проверкой самого себя.

Берутся ТОЛЬКО идентификаторы. Ни названий, ни жанров, ни описаний: каталог
принадлежит Movies API, и вторая его копия начала бы расходиться с первой в
день первого переименования.
"""

import logging

from sqlalchemy import create_engine, text

from practix_recsys_trainer.core.config import settings

logger = logging.getLogger(__name__)

# Схема content — та же инъекция схемы, что в django-админке
# (`db_table = 'content"."film_work'`).
FILMS_QUERY = 'SELECT id FROM content.film_work ORDER BY id'


def fetch_film_ids(limit: int | None = None) -> list[str]:
    """Идентификаторы фильмов каталога.

    Отказ источника — пустой список и предупреждение. Последствия честные и
    ограниченные: без снимка каталога выдача не сможет отличить неизвестный
    фильм от фильма без соседей и ответит популярным на оба случая — то есть
    деградирует ровно так, как задумано, вместо того чтобы уронить прогон.
    """
    engine = create_engine(settings.catalog_dsn, pool_pre_ping=True)
    query = FILMS_QUERY if limit is None else f'{FILMS_QUERY} LIMIT {int(limit)}'
    try:
        with engine.connect() as conn:
            return [str(row[0]) for row in conn.execute(text(query)).all()]
    except Exception as exc:  # noqa: BLE001 — каталог недоступен: см. докстринг
        logger.warning('CATALOG UNAVAILABLE: витрина останется без снимка каталога (%s)', exc)
        return []
    finally:
        engine.dispose()
