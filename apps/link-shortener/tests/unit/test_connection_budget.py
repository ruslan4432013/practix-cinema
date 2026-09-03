"""Соединений к shortener-db не бывает больше, чем принимает shortener-db.

Та же арифметика, что уже поймали в выдаче рекомендаций и в ugc-api. Пул
SQLAlchemy принадлежит ПРОЦЕССУ, а ``max_connections`` — БАЗЕ, и между ними
стоит множитель, которого не видно ни в одном из двух файлов: число воркеров
uvicorn живёт в Dockerfile. Настройки 10 + 20 на воркер означали 120 соединений
против умолчания postgres в 100.

Здесь это дороже, чем в соседних сервисах: ``/s/{code}`` — маршрут ИЗ ПИСЬМА, и
его отказ человек читает как «ссылка не работает», то есть как сломанное
подтверждение адреса. Причём отказ пришёл бы именно в пик — сразу после массовой
рассылки, когда по ссылкам ходят все сразу.

Арифметика и разбор Dockerfile, compose и ``.env.example`` живут в
``practix_testing.deployment`` — здесь только числа этого сервиса.
"""

from practix_link_shortener.core.config import Settings
from practix_testing.deployment import (
    assert_code_defaults_fit_database,
    assert_pool_fits_database,
)

TARGET = 'link-shortener'
DATABASE = 'shortener-db'
POOL_KEY = 'SHORTENER_DB_POOL_SIZE'
OVERFLOW_KEY = 'SHORTENER_DB_MAX_OVERFLOW'

# Потолки соседей по shortener-db: уборка протухших ссылок (`cli purge`) берёт
# движок приложения, то есть его же пул; миграции — разовый alembic.
PURGE_CEILING = 10
MIGRATIONS_CEILING = 2


def test_all_workers_together_fit_into_the_database_limit():
    """Считать надо на контейнер: воркеров четыре, пул у каждого свой."""
    assert_pool_fits_database(TARGET, DATABASE, POOL_KEY, OVERFLOW_KEY)


def test_the_cleanup_can_still_connect_while_the_redirect_is_hot():
    """Уборка обязана подключаться и тогда, когда по ссылкам ходят все сразу.

    Иначе протухшие строки перестают удаляться ровно в тот период, когда их
    больше всего, — и «404 по истёкшей ссылке» превращается в рост таблицы,
    которого никто не заметит, потому что редирект при этом исправен.
    """
    assert_pool_fits_database(
        TARGET,
        DATABASE,
        POOL_KEY,
        OVERFLOW_KEY,
        neighbours=PURGE_CEILING + MIGRATIONS_CEILING,
    )


def test_code_defaults_are_not_looser_than_the_shipped_template():
    """Умолчания кода — тоже боевые значения: ключ из .env.example легко удалить."""
    assert_code_defaults_fit_database(TARGET, DATABASE, POOL_KEY, OVERFLOW_KEY, Settings)
