"""
Скрипт для заполнения PostgreSQL тестовыми данными с использованием Faker:
  - 110 жанров
  - 5 000 персон (актёры, режиссёры, сценаристы)
  - 200 000 фильмов
  - связи film_work <-> genre и film_work <-> person

БД: PostgreSQL, схема content (см. schema_design/movies_database.ddl)
ETL читает именно эту БД и переносит данные в Elasticsearch.

Запуск (пока контейнеры подняты):
    python seed_db.py

Зависимости:
    pip install psycopg2-binary faker
"""

import os
import random
import uuid
from datetime import UTC, datetime

import psycopg2
from faker import Faker
from psycopg2.extras import execute_values

# ---------------------------------------------------------------------------
# Настройки подключения — берём из переменных окружения или дефолты из .env
# ---------------------------------------------------------------------------
PG_DSL = {
    'dbname': os.getenv('POSTGRES_DB', 'theatre'),
    'user': os.getenv('POSTGRES_USER', 'postgres'),
    'password': os.getenv('POSTGRES_PASSWORD', 'secret'),
    'host': os.getenv('POSTGRES_HOST', 'localhost'),
    'port': int(os.getenv('POSTGRES_PORT', '5432')),
}

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------
NUM_GENRES = 110
NUM_PERSONS = 5_000
NUM_FILMS = 200_000
BATCH_SIZE = 5_000
GENRES_PER_FILM = (1, 4)
PERSONS_PER_FILM = (2, 15)

NOW = datetime.now(tz=UTC)

FILM_TYPES = ['movie', 'tv_show']
ROLES = ['actor', 'director', 'writer']

# Список жанров (110 штук)
GENRE_NAMES = [
    'Action',
    'Adventure',
    'Animation',
    'Biography',
    'Comedy',
    'Crime',
    'Documentary',
    'Drama',
    'Family',
    'Fantasy',
    'Film-Noir',
    'History',
    'Horror',
    'Music',
    'Musical',
    'Mystery',
    'Romance',
    'Sci-Fi',
    'Short',
    'Sport',
    'Superhero',
    'Thriller',
    'War',
    'Western',
    'Anime',
    'Arthouse',
    'Black Comedy',
    'Buddy Film',
    'Chick Flick',
    'Cyberpunk',
    'Dark Fantasy',
    'Disaster',
    'Dystopian',
    'Erotic',
    'Espionage',
    'Experimental',
    'Fairy Tale',
    'Found Footage',
    'Gangster',
    'Gothic',
    'Heist',
    'Holiday',
    'Indie',
    'Juvenile',
    'Kaiju',
    'Legal Drama',
    'Martial Arts',
    'Medical Drama',
    'Melodrama',
    'Military',
    'Mockumentary',
    'Monster',
    'Neo-Noir',
    'Paranormal',
    'Period Drama',
    'Philosophical',
    'Political',
    'Post-Apocalyptic',
    'Psychological',
    'Road Movie',
    'Satire',
    'Slasher',
    'Space Opera',
    'Steampunk',
    'Supernatural',
    'Surreal',
    'Survival',
    'Sword and Sorcery',
    'Teen Drama',
    'Time Travel',
    'Tragedy',
    'Vampire',
    'Vigilante',
    'Zombie',
    'Absurdist',
    'Biographical Drama',
    'Body Horror',
    'Buddy Comedy',
    'Cape Fear',
    'Caper',
    'Chanbara',
    'Conspiracy',
    'Cop Drama',
    'Courtroom Drama',
    'Dance',
    'Dark Comedy',
    'Detective',
    'Docudrama',
    'Eco-Thriller',
    'Epic',
    'Erotic Thriller',
    'Exploitation',
    'Fable',
    'Folk Horror',
    'Giallo',
    'Grindhouse',
    'Hardboiled',
    'Heroic Fantasy',
    'Historical Fiction',
    'Humanist',
    'Hyper-Reality',
    'Immigrant Drama',
    'Jungle Adventure',
    'Kitchen Sink',
    'LGBTQ+',
    'Lovecraftian',
    'Magical Realism',
    'Mumblecore',
    'Mythological',
    'Nature Documentary',
    'Noir Comedy',
    'Occult',
][:NUM_GENRES]


# ---------------------------------------------------------------------------
# Основная логика
# ---------------------------------------------------------------------------


def seed(conn: psycopg2.extensions.connection) -> None:
    fake = Faker()
    Faker.seed(42)
    random.seed(42)

    cur = conn.cursor()

    # -----------------------------------------------------------------------
    # 1. Жанры
    # -----------------------------------------------------------------------
    print(f'Вставляем {NUM_GENRES} жанров...')
    genre_ids = [str(uuid.uuid4()) for _ in range(NUM_GENRES)]
    genre_rows = [(genre_ids[i], GENRE_NAMES[i], fake.sentence(nb_words=8), NOW, NOW) for i in range(NUM_GENRES)]
    execute_values(
        cur,
        """
        INSERT INTO content.genre (id, name, description, created, modified)
        VALUES %s
        ON CONFLICT DO NOTHING
        """,
        genre_rows,
    )
    conn.commit()
    print(f'  ✓ Жанры вставлены: {len(genre_ids)}')

    # -----------------------------------------------------------------------
    # 2. Персоны
    # -----------------------------------------------------------------------
    print(f'Вставляем {NUM_PERSONS} персон...')
    person_ids = [str(uuid.uuid4()) for _ in range(NUM_PERSONS)]
    person_rows = [(person_ids[i], fake.name(), NOW, NOW) for i in range(NUM_PERSONS)]
    for offset in range(0, NUM_PERSONS, BATCH_SIZE):
        execute_values(
            cur,
            """
            INSERT INTO content.person (id, full_name, created, modified)
            VALUES %s
            ON CONFLICT DO NOTHING
            """,
            person_rows[offset : offset + BATCH_SIZE],
        )
    conn.commit()
    print(f'  ✓ Персоны вставлены: {len(person_ids)}')

    # -----------------------------------------------------------------------
    # 3. Фильмы + связи (батчами, чтобы не упасть по памяти)
    # -----------------------------------------------------------------------
    print(f'Вставляем {NUM_FILMS} фильмов (батчами по {BATCH_SIZE})...')

    total_films = 0
    total_genre_links = 0
    total_person_links = 0

    for batch_start in range(0, NUM_FILMS, BATCH_SIZE):
        batch_end = min(batch_start + BATCH_SIZE, NUM_FILMS)
        batch_count = batch_end - batch_start

        film_rows = []
        genre_link_rows = []
        person_link_rows = []

        seen_genre_links: set[tuple[str, str]] = set()
        seen_person_links: set[tuple[str, str, str]] = set()

        for i in range(batch_start, batch_end):
            film_id = str(uuid.uuid4())
            title = fake.catch_phrase() + f' #{i + 1}'
            description = fake.paragraph(nb_sentences=3)
            creation_dt = fake.date_between(start_date='-100y', end_date='today')
            rating = round(random.uniform(1.0, 10.0), 1)
            film_type = random.choice(FILM_TYPES)
            modified = fake.date_time_between(start_date='-5y', end_date='now', tzinfo=UTC)

            film_rows.append(
                (
                    film_id,
                    title,
                    description,
                    creation_dt,
                    rating,
                    film_type,
                    NOW,
                    modified,
                )
            )

            # Жанры фильма
            num_genres = random.randint(*GENRES_PER_FILM)
            chosen_genres = random.sample(genre_ids, min(num_genres, len(genre_ids)))
            for gid in chosen_genres:
                key = (gid, film_id)
                if key not in seen_genre_links:
                    seen_genre_links.add(key)
                    genre_link_rows.append((str(uuid.uuid4()), gid, film_id, NOW))

            # Персоны фильма
            num_persons = random.randint(*PERSONS_PER_FILM)
            chosen_persons = random.sample(person_ids, min(num_persons, len(person_ids)))
            for pid in chosen_persons:
                role = random.choice(ROLES)
                key = (film_id, pid, role)
                if key not in seen_person_links:
                    seen_person_links.add(key)
                    person_link_rows.append((str(uuid.uuid4()), film_id, pid, role, NOW))

        # INSERT фильмов
        execute_values(
            cur,
            """
            INSERT INTO content.film_work
                (id, title, description, creation_date, rating, type, created, modified)
            VALUES %s
            ON CONFLICT DO NOTHING
            """,
            film_rows,
        )

        # INSERT genre_film_work
        if genre_link_rows:
            execute_values(
                cur,
                """
                INSERT INTO content.genre_film_work (id, genre_id, film_work_id, created)
                VALUES %s
                ON CONFLICT DO NOTHING
                """,
                genre_link_rows,
            )

        # INSERT person_film_work
        if person_link_rows:
            execute_values(
                cur,
                """
                INSERT INTO content.person_film_work (id, film_work_id, person_id, role, created)
                VALUES %s
                ON CONFLICT DO NOTHING
                """,
                person_link_rows,
            )

        conn.commit()

        total_films += batch_count
        total_genre_links += len(genre_link_rows)
        total_person_links += len(person_link_rows)

        if total_films % 50_000 == 0 or total_films == NUM_FILMS:
            print(
                f'  → Фильмов: {total_films:,} | '
                f'Связей жанров: {total_genre_links:,} | '
                f'Связей персон: {total_person_links:,}'
            )

    cur.close()
    print('\n✅ Готово!')
    print(f'   Жанров:          {NUM_GENRES:,}')
    print(f'   Персон:          {NUM_PERSONS:,}')
    print(f'   Фильмов:         {total_films:,}')
    print(f'   Связей жанров:   {total_genre_links:,}')
    print(f'   Связей персон:   {total_person_links:,}')


def main() -> None:
    print('Подключаемся к PostgreSQL...')
    print(f'  host={PG_DSL["host"]}  db={PG_DSL["dbname"]}  user={PG_DSL["user"]}')
    conn = psycopg2.connect(**PG_DSL)
    try:
        seed(conn)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
