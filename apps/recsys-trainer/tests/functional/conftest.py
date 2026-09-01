"""Фикстуры набора батча: живой одноузловой ClickHouse и живая витрина.

Оба источника настоящие. Подменять ClickHouse заглушкой значило бы не проверить
главное — что запрос к ``ugc.film_views`` действительно возвращает то, из чего
строится матрица, а витрина действительно принимает то, что раскладывает
писатель.

Схему витрины накатывает одноразовый контейнер ``recs-migrations-test`` (см.
docker-compose.test.yml): миграции живут в пакете выдачи, а в образе батча его
нет. Здесь только очистка между тестами.

Набор синхронный: и ClickHouse, и витрина читаются синхронными драйверами —
батч линеен по природе, и event loop ему негде применить.
"""

import datetime
import uuid

import pytest
from sqlalchemy import create_engine, text

from practix_recsys_trainer.core.config import settings
from practix_recsys_trainer.dataset import sinks
from practix_recsys_trainer.dataset.synthetic import ViewSession
from practix_recsys_trainer.sources import clickhouse

# Списки чистятся TRUNCATE, версии — DELETE. Разница существенная: на
# shelf_version ссылается shelf_pointer, и TRUNCATE ... CASCADE вычистил бы
# заодно и его — вместе с ЕДИНСТВЕННОЙ строкой указателя, которую создаёт
# миграция. Дальше writer.publish обновлял бы ноль строк, указатель никуда не
# вставал, и все проверки публикации падали бы на пустом месте.
ITEM_TABLES = ('similar_item', 'personal_item', 'popular_item', 'catalog_film')


@pytest.fixture
def ch_client():
    client = clickhouse.connect()
    # TRUNCATE идёт по *_local, а не по Distributed-обёртке: у обёртки нет
    # собственных данных, и очистка через неё на одноузловом стенде молча
    # ничего не делает. Тот же приём в наборе ETL.
    client.command('TRUNCATE TABLE IF EXISTS ugc.film_views_local')
    yield client
    client.close()


@pytest.fixture
def shelf_engine():
    engine = create_engine(settings.shelf_dsn, pool_pre_ping=True, future=True)
    with engine.begin() as conn:
        conn.execute(text('UPDATE shelf_pointer SET version = NULL WHERE id = true'))
        for table in ITEM_TABLES:
            conn.execute(text(f'TRUNCATE TABLE {table}'))
        conn.execute(text('DELETE FROM shelf_version'))
        # Указатель обязан существовать: его создаёт миграция, а его отсутствие
        # неотличимо от «обучение ещё не проходило» и молча ломает весь набор.
        conn.execute(
            text(
                'INSERT INTO shelf_pointer (id, version) VALUES (true, NULL) '
                'ON CONFLICT (id) DO UPDATE SET version = NULL'
            )
        )
    yield engine
    engine.dispose()


@pytest.fixture
def catalog(monkeypatch) -> list[str]:
    """Каталог подменяется: theatre-db на этом стенде нет.

    Идентификаторы всё равно настоящие UUID той же формы — витрина хранит их
    как ``uuid``, и подмена на строки вида 'film-1' проверяла бы не тот путь.
    """
    film_ids = [str(uuid.UUID(int=index)) for index in range(1, 61)]
    monkeypatch.setattr('practix_recsys_trainer.sources.catalog.fetch_film_ids', lambda limit=None: list(film_ids))
    monkeypatch.setattr('practix_recsys_trainer.pipeline.catalog.fetch_film_ids', lambda limit=None: list(film_ids))
    return film_ids


@pytest.fixture
def seed_views(ch_client, catalog):
    """Кладёт в ClickHouse историю просмотров с ЯВНОЙ структурой.

    Не генератор: набору нужен предсказуемый ответ, а не правдоподобный. Две
    группы зрителей смотрят две непересекающиеся половины каталога — значит
    соседи обязаны находиться внутри своей половины, и это проверяемо глазами.
    """

    def build(*, users_per_group: int = 12, films_per_group: int = 6) -> dict:
        now = datetime.datetime.now(datetime.UTC)
        group_a = catalog[:films_per_group]
        group_b = catalog[films_per_group : films_per_group * 2]

        sessions: list[ViewSession] = []
        watched: dict[str, set[str]] = {}
        for group_index, films in enumerate((group_a, group_b)):
            for user_index in range(users_per_group):
                user_id = str(uuid.UUID(int=1000 + group_index * 100 + user_index))
                watched.setdefault(user_id, set()).update(films)
                for film_id in films:
                    sessions.append(
                        ViewSession(
                            user_id=user_id,
                            film_id=film_id,
                            session_id=f'seed-{group_index}-{user_index}',
                            started_at=now - datetime.timedelta(days=1),
                            duration_ms=6_000_000,
                            completion_rate=0.95,
                            quality='1080p',
                            device_type='desktop',
                        )
                    )

        sinks.to_clickhouse(ch_client, sessions)
        # Distributed-вставка подтверждена синхронно (distributed_foreground_insert),
        # но ReplacingMergeTree отдаёт строки только после записи парта — на
        # одноузловом стенде это происходит сразу, проверяем явно.
        written = ch_client.query('SELECT count() FROM ugc.film_views').result_rows[0][0]
        assert written > 0, 'история просмотров не доехала до ClickHouse'
        # Историю возвращаем ту, которую сами и построили. Перечитывать её из
        # ClickHouse значило бы проверять модель против результата ещё одного
        # запроса, а не против того, что ей скормили.
        return {'group_a': group_a, 'group_b': group_b, 'watched': watched}

    return build
