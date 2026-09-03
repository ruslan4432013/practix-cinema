"""Соединений к ugc-db не бывает больше, чем принимает ugc-db.

Та же арифметика, что уже поймали в выдаче рекомендаций, не сходилась и здесь.
Пул SQLAlchemy принадлежит ПРОЦЕССУ, а ``max_connections`` — БАЗЕ, и между ними
стоит множитель, которого не видно ни в одном из двух файлов: число воркеров
uvicorn живёт в Dockerfile. Настройки 10 + 20 на воркер выглядели скромно и
означали 120 соединений против умолчания postgres в 100 — под нагрузкой оценка
и закладка получали бы `too many connections` вместо ответа. Докстринг
``db/postgres.py`` при этом объяснял, что пул ограничен «явно, потому что
умолчание 5 + 10 упирается в max_connections», и задавал ВТРОЕ больший потолок:
комментарий описывал намерение, которого числа не исполняли, и читался при этом
как «уже подумали».

Арифметика и разбор Dockerfile, compose и ``.env.example`` живут в
``practix_testing.deployment`` — здесь только числа этого сервиса.
"""

from practix_testing.deployment import (
    assert_code_defaults_fit_database,
    assert_pool_fits_database,
)
from practix_ugc_api.core.config import Settings

TARGET = 'ugc-api'
DATABASE = 'ugc-db'
POOL_KEY = 'UGC_API_DB_POOL_SIZE'
OVERFLOW_KEY = 'UGC_API_DB_MAX_OVERFLOW'

# Потолки соседей по ugc-db. Батч рекомендаций читает отсюда оценки как
# дополнительный сигнал (sources/ratings.py у recsys-trainer) движком SQLAlchemy
# с умолчаниями 5 + 10; `cli recount` берёт движок приложения, то есть его же
# пул; миграции — разовый alembic.
TRAINER_CEILING = 15
RECOUNT_CEILING = 10
MIGRATIONS_CEILING = 2


def test_all_workers_together_fit_into_the_database_limit():
    """Считать надо на контейнер: воркеров четыре, пул у каждого свой."""
    assert_pool_fits_database(TARGET, DATABASE, POOL_KEY, OVERFLOW_KEY)


def test_the_neighbours_still_have_room():
    """База не только у сервиса: сюда же ходят батч, пересчёт и миграции.

    Занять весь лимит горячим путём значило бы, что расхождение преагрегата
    нечем пересчитать, а ночное обучение останется без явного сигнала, — и оба
    отказа выглядели бы как проблема совсем другого сервиса.
    """
    assert_pool_fits_database(
        TARGET,
        DATABASE,
        POOL_KEY,
        OVERFLOW_KEY,
        neighbours=TRAINER_CEILING + RECOUNT_CEILING + MIGRATIONS_CEILING,
    )


def test_code_defaults_are_not_looser_than_the_shipped_template():
    """Умолчания кода — тоже боевые значения: ключ из .env.example легко удалить."""
    assert_code_defaults_fit_database(TARGET, DATABASE, POOL_KEY, OVERFLOW_KEY, Settings)
