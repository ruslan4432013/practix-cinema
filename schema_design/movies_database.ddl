CREATE SCHEMA IF NOT EXISTS content;

CREATE TABLE IF NOT EXISTS content.film_work
(
    id            uuid PRIMARY KEY,
    title         VARCHAR(255) NOT NULL,
    description   TEXT,
    creation_date DATE,
    rating        NUMERIC(3, 1)
        CONSTRAINT check_rating CHECK (rating >= 0 AND rating <= 10),
    type          VARCHAR(64)  NOT NULL,
    created       timestamp with time zone,
    modified      timestamp with time zone
);

CREATE TABLE IF NOT EXISTS content.genre
(
    id          uuid PRIMARY KEY,
    name        varchar(255) NOT NULL,
    description TEXT,
    created     timestamp with time zone,
    modified    timestamp with time zone
);

CREATE TABLE IF NOT EXISTS content.person
(
    id        uuid PRIMARY KEY,
    full_name varchar(255) NOT NULL,
    created   timestamp with time zone,
    modified  timestamp with time zone
);

CREATE TABLE IF NOT EXISTS content.person_film_work
(
    id           uuid PRIMARY KEY,
    film_work_id uuid        NOT NULL,
    person_id    uuid        NOT NULL,
    role         VARCHAR(64) NOT NULL,
    created      timestamp with time zone,

    CONSTRAINT fk_film_work_id
        FOREIGN KEY (film_work_id)
            REFERENCES content.film_work (id)
            ON DELETE CASCADE,

    CONSTRAINT fk_person_id
        FOREIGN KEY (person_id)
            REFERENCES content.person (id)
            ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS content.genre_film_work
(
    id           uuid PRIMARY KEY,
    genre_id     uuid NOT NULL,
    film_work_id uuid NOT NULL,
    created      timestamp with time zone,
    CONSTRAINT fk_genre_id
        FOREIGN KEY (genre_id)
            REFERENCES content.genre (id)
            ON DELETE CASCADE,
    CONSTRAINT fk_film_work_id
        FOREIGN KEY (film_work_id)
            REFERENCES content.film_work (id)
            ON DELETE CASCADE
);


CREATE UNIQUE INDEX film_work_person_role_idx
    ON content.person_film_work (film_work_id, person_id, role);

CREATE UNIQUE INDEX genre_film_work_idx ON content.genre_film_work (genre_id, film_work_id);

CREATE INDEX film_work_title_idx ON content.film_work (title);

CREATE INDEX person_full_name_idx ON content.person (full_name);
