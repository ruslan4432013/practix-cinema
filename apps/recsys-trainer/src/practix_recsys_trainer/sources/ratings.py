"""Явный сигнал: оценки 0..10 из ugc-db — дополнительный вес, а не основа.

ADR-005 отводит оценкам роль добавки: их на порядки меньше просмотров и они
смещены (оценку ставит тот, у кого сильные эмоции — в обе стороны). Учить
ТОЛЬКО на них значило бы строить модель по самой шумной и самой
нерепрезентативной части аудитории.

Поэтому оценка не создаёт взаимодействия, а лишь усиливает или ослабляет уже
существующее: человек, досмотревший фильм и поставивший десятку, — сигнал
сильнее, чем просто досмотревший. Пара «оценка есть, просмотра нет» в матрицу
не попадает: у неё нет подтверждения поведением.

Читаем базу соседнего сервиса напрямую, и это не нарушение принципа «сервис
владеет своими данными»: точно так же ``etl-elasticsearch`` читает
``theatre-db`` django-админки. Доступ офлайновый, соединение read-only, на
горячем пути его нет — а ручки «выгрузи все оценки» у ugc-api не существует, и
заводить её ради ночного батча значило бы открыть наружу то, что нужно одному
внутреннему потребителю.
"""

import logging

from sqlalchemy import create_engine, text

from practix_recsys_trainer.core.config import settings

logger = logging.getLogger(__name__)


def fetch_ratings() -> dict[tuple[str, str], int]:
    """Оценки как отображение (user_id, film_id) -> 0..10.

    Отказ источника — предупреждение, а не падение прогона: без добавки модель
    хуже, без просмотров — невозможна. Ронять ночное обучение из-за
    недоступности вторичного сигнала было бы обменом наоборот.
    """
    if not settings.RECS_TRAINER_RATINGS_ENABLED:
        return {}
    engine = create_engine(settings.ratings_dsn, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text('SELECT user_id, film_id, rating FROM likes')).all()
    except Exception as exc:  # noqa: BLE001 — вторичный сигнал не имеет права ронять прогон
        logger.warning('RATINGS UNAVAILABLE: учимся без явного сигнала (%s)', exc)
        return {}
    finally:
        engine.dispose()
    return {(str(row[0]), str(row[1])): int(row[2]) for row in rows}


def count_ratings() -> int:
    """Сколько оценок есть — для инвентаризации сигналов (T2.1)."""
    if not settings.RECS_TRAINER_RATINGS_ENABLED:
        return 0
    engine = create_engine(settings.ratings_dsn, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            return int(conn.execute(text('SELECT count(*) FROM likes')).scalar_one())
    except Exception as exc:  # noqa: BLE001 — источника может не быть на стенде
        logger.warning('RATINGS UNAVAILABLE: %s', exc)
        return 0
    finally:
        engine.dispose()
