"""Соединений к витрине не бывает больше, чем принимает витрина.

Регрессия. Пул SQLAlchemy принадлежит ПРОЦЕССУ, а ``max_connections`` — БАЗЕ, и
между ними стоит множитель, которого не видно ни в одном из двух файлов: число
воркеров uvicorn живёт в Dockerfile. Настройки 10 + 20 на воркер выглядели
скромно и означали 120 соединений против 100 у recs-db: под нагрузкой запросы
получали бы `too many connections` — то есть отказ, которого никто не заказывал,
ровно тогда, когда выдача нужнее всего.

Сама арифметика и разбор Dockerfile, compose и ``.env.example`` живут в
``practix_testing.deployment``: та же проверка нужна ugc-api и link-shortener, и
третья копия разбора перевела бы `npm run dup` через порог. Здесь остаются
числа этого сервиса и утверждения о том, чем именно рискуют.
"""

from practix_recommendations_api.core.config import Settings
from practix_testing.deployment import (
    assert_code_defaults_fit_database,
    assert_pool_fits_database,
    settings_default,
)

TARGET = 'recommendations-api'
DATABASE = 'recs-db'
POOL_KEY = 'RECS_DB_POOL_SIZE'
OVERFLOW_KEY = 'RECS_DB_MAX_OVERFLOW'

# Потолок сервисов, делящих витрину с выдачей: батч (движок SQLAlchemy по
# умолчанию, 5 + 10) и разовые миграции. Батч живёт в профиле warehouse, но
# считать надо худший случай — профиль включён.
TRAINER_CEILING = 15
MIGRATIONS_CEILING = 2


def test_all_workers_together_fit_into_the_database_limit():
    """Считать надо на контейнер: воркеров четыре, пул у каждого свой."""
    assert_pool_fits_database(TARGET, DATABASE, POOL_KEY, OVERFLOW_KEY)


def test_the_neighbours_of_the_shelf_still_have_room():
    """Витрина не только у выдачи: в ту же базу пишет батч и ходят миграции.

    Занять весь лимит выдачей значило бы, что ночное обучение не сможет
    подключиться, — и витрина перестанет обновляться, оставаясь при этом
    формально исправной.
    """
    assert_pool_fits_database(
        TARGET,
        DATABASE,
        POOL_KEY,
        OVERFLOW_KEY,
        neighbours=TRAINER_CEILING + MIGRATIONS_CEILING,
    )


def test_code_defaults_are_not_looser_than_the_shipped_template():
    """Умолчания кода — тоже боевые значения: ключ из .env.example легко удалить.

    Именно эта проверка поймала первую версию правки: код уже был исправлен, а
    стенд продолжал жить со старыми 10 + 20 из шаблона.
    """
    assert_code_defaults_fit_database(TARGET, DATABASE, POOL_KEY, OVERFLOW_KEY, Settings)


def test_waiting_for_a_free_connection_is_bounded_by_the_request_budget():
    """Исчерпанный пул — это ожидание, а не отказ, и ждать оно обязано недолго.

    Ровно та же ловушка, что с погашенным контейнером: без ``pool_timeout``
    запрос висел бы в очереди за соединением, ``except`` в лестнице деградации
    не выполнился бы, и вместо 200 с популярным пользователь получил бы 504 от
    nginx. ``pool_timeout`` задан в db/postgres.py тем же RECS_DB_TIMEOUT.

    Сравниваются ОБЪЯВЛЕННЫЕ умолчания, а не собранные настройки: nx подгружает
    корневой ``.env`` разработчика в окружение задачи, и тест мерил бы его
    стенд вместо репозитория.
    """
    timeout = settings_default(Settings, 'RECS_DB_TIMEOUT')
    budget = settings_default(Settings, 'RECS_REQUEST_BUDGET')

    assert timeout < budget
