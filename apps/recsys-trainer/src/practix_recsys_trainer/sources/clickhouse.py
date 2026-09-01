"""Неявный сигнал: просмотры из ClickHouse — основной источник обучения.

ADR-005: учим на просмотрах, а не на оценках. Оценок на порядки меньше, и они
смещены — их ставят те, у кого сильные эмоции. Просмотр же есть у каждого, кто
что-то посмотрел, и именно он отвечает на вопрос «какие фильмы смотрят одни и
те же люди».

Читается ``ugc.film_views`` — типизированная таблица, которую ETL наполняет из
``video_progress`` и ``video_completed``. Разбирать JSON из ``ugc.raw_events``
незачем: всё нужное здесь уже разложено по колонкам.

СЧИТАЕМ ``uniq(view_id)``, А НЕ ``count()``. Метки прогресса идут десятками на
один просмотр, и ``count()`` измерял бы длину фильма, а не его популярность.
Побочный, но важный эффект: ``uniq(view_id)`` невосприимчив к повторной
доставке по построению — а таблица лежит на ``ReplacingMergeTree``, где дубли
до слияния видны (см. ``apps/etl-clickhouse/docs/analytics_schema.md``).

Анонимные строки (``user_id IS NULL``) отбрасываются: коллаборативная
фильтрация строится по людям, а ``session_id`` — это не человек, а вкладка.
"""

import datetime
import logging
from dataclasses import dataclass

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from practix_recsys_trainer.core.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Interaction:
    """Одно взаимодействие «пользователь ↔ фильм», уже свёрнутое по сеансам."""

    user_id: str
    film_id: str
    weight: float
    last_seen: datetime.datetime


@dataclass(frozen=True, slots=True)
class SignalStats:
    """Инвентаризация сигналов — ответ на вопрос «сколько данных есть на самом деле»."""

    rows: int
    views: int
    users: int
    films: int
    active_users: int
    first_event: datetime.datetime | None
    last_event: datetime.datetime | None


def connect() -> Client:
    """Первый живой узел из ``CH_HOSTS``.

    Кворум чтения батчу не нужен — нужен любой узел, отвечающий на SELECT:
    Distributed-таблица сама сходит по шардам. Поэтому перебор, а не отказ на
    первом недоступном адресе: одна упавшая реплика не должна отменять ночное
    обучение.
    """
    last_error: Exception | None = None
    for host, port in settings.clickhouse_endpoints:
        try:
            client = clickhouse_connect.get_client(
                host=host,
                port=port,
                username=settings.CH_USER,
                password=settings.CH_PASSWORD,
                database=settings.CH_DATABASE,
                connect_timeout=settings.CH_CONNECT_TIMEOUT,
                send_receive_timeout=settings.CH_SEND_RECEIVE_TIMEOUT,
            )
            client.query('SELECT 1')
        except Exception as exc:  # noqa: BLE001 — пробуем следующий адрес
            logger.warning('CLICKHOUSE NODE UNAVAILABLE: %s:%s (%s)', host, port, exc)
            last_error = exc
            continue
        return client
    raise RuntimeError(f'Ни один узел ClickHouse не ответил: {settings.CH_HOSTS}') from last_error


def fetch_interactions(client: Client, *, since: datetime.datetime | None = None) -> list[Interaction]:
    """Матрица «пользователь × фильм» в длинном формате.

    Вес — максимальная доля просмотра по всем сеансам пары: человек, бросивший
    фильм на десятой минуте и вернувшийся досмотреть, посмотрел его целиком, а
    ``avg`` по меткам прогресса сказал бы «наполовину».

    ``last_seen`` нужен сплиту по времени (T2.4): отложенная выборка делается по
    времени, а не случайно, иначе модель обучится на будущем и метрики качества
    окажутся завышенными — ровно та ошибка, ради недопущения которой E5 стоит
    в плане до E6.
    """
    where = ['user_id IS NOT NULL']
    params: dict[str, object] = {
        'min_completion': settings.RECS_TRAINER_MIN_COMPLETION,
        'limit': settings.RECS_TRAINER_MAX_INTERACTIONS,
    }
    if since is not None:
        where.append('event_time >= %(since)s')
        params['since'] = since

    query = f"""
        SELECT toString(user_id) AS user_id,
               toString(film_id)  AS film_id,
               max(completion_rate) AS weight,
               max(event_time)      AS last_seen
        FROM film_views
        WHERE {' AND '.join(where)}
        GROUP BY user_id, film_id
        HAVING weight >= %(min_completion)s
        ORDER BY last_seen
        LIMIT %(limit)s
    """
    rows = client.query(query, parameters=params).result_rows
    return [Interaction(user_id=row[0], film_id=row[1], weight=float(row[2]), last_seen=row[3]) for row in rows]


def fetch_popular(client: Client, *, limit: int, window_days: int) -> list[tuple[str, float]]:
    """Топ фильмов за окно по числу СЕАНСОВ просмотра.

    Окно настраиваемое (по умолчанию 30 дней, F1.1): «популярное за всё время»
    — это витрина премий, а не подсказка, что посмотреть сегодня.
    """
    query = """
        SELECT toString(film_id) AS film_id, uniq(view_id) AS views
        FROM film_views
        WHERE event_time >= now() - INTERVAL %(days)s DAY
        GROUP BY film_id
        ORDER BY views DESC, film_id
        LIMIT %(limit)s
    """
    rows = client.query(query, parameters={'days': window_days, 'limit': limit}).result_rows
    return [(row[0], float(row[1])) for row in rows]


def fetch_stats(client: Client) -> SignalStats:
    """Сколько данных есть на самом деле (T2.1).

    Первая команда, которую стоит выполнить на новом стенде: если активных
    пользователей единицы, обучать нечего, и это надо узнать до того, как
    четыре часа ушло на отладку модели, а не после.
    """
    query = """
        SELECT count()                    AS rows,
               uniq(view_id)              AS views,
               uniqIf(user_id, user_id IS NOT NULL) AS users,
               uniq(film_id)              AS films,
               min(event_time)            AS first_event,
               max(event_time)            AS last_event
        FROM film_views
    """
    row = client.query(query).result_rows[0]

    # Пользователи, у которых есть хотя бы пара фильмов, — единственные, кто
    # вообще участвует в со-встречаемости. Остальные раздувают матрицу и
    # улучшают статистику, не улучшая модель.
    active_query = """
        SELECT count() FROM (
            SELECT user_id
            FROM film_views
            WHERE user_id IS NOT NULL AND completion_rate >= %(min_completion)s
            GROUP BY user_id
            HAVING uniq(film_id) >= %(min_events)s
        )
    """
    active = client.query(
        active_query,
        parameters={
            'min_completion': settings.RECS_TRAINER_MIN_COMPLETION,
            'min_events': settings.RECS_TRAINER_MIN_USER_EVENTS,
        },
    ).result_rows[0][0]

    return SignalStats(
        rows=int(row[0]),
        views=int(row[1]),
        users=int(row[2]),
        films=int(row[3]),
        active_users=int(active),
        first_event=row[4],
        last_event=row[5],
    )
