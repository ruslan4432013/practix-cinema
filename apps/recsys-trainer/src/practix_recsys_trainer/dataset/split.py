"""Сплит train/test ПО ВРЕМЕНИ, а не случайной выборкой (T2.4).

Случайный сплит в рекомендациях — классическая ошибка, дающая красивые и
бессмысленные метрики. Он разрешает модели учиться на том, что пользователь
посмотрит завтра, и предсказывать то, что он посмотрел вчера; в бою такой
возможности нет никогда, и качество на проде оказывается вдвое ниже
отчётного. Именно поэтому DoD эпика E2 требует сплит по времени явно.

Граница выбирается как квантиль по времени событий, а не как фиксированная
дата: на синтетическом стенде «последние 7 дней» могут не содержать вообще
ничего, и тест окажется пустым, а метрики — неопределёнными.
"""

from dataclasses import dataclass

from practix_recsys_trainer.sources.clickhouse import Interaction

DEFAULT_TEST_FRACTION = 0.2


@dataclass(frozen=True, slots=True)
class TimeSplit:
    """Обучающая и отложенная выборки плюс граница, по которой они разошлись."""

    train: list[Interaction]
    test: list[Interaction]
    boundary_index: int

    @property
    def is_usable(self) -> bool:
        """Годится ли сплит для оценки качества.

        Пустая тестовая часть — не ошибка, а исход: данных так мало, что мерить
        нечего. Метрики в этом случае честнее не считать, чем посчитать по
        одному наблюдению и напечатать «precision@10 = 1.0».
        """
        return bool(self.train) and bool(self.test)


def split_by_time(
    interactions: list[Interaction],
    *,
    test_fraction: float = DEFAULT_TEST_FRACTION,
) -> TimeSplit:
    """Всё до границы — обучение, всё после — проверка.

    Сортировка стабильна по паре (время, пользователь, фильм): одинаковые метки
    времени встречаются на синтетике сплошь и рядом, и без вторичного ключа
    порядок зависел бы от порядка выдачи ClickHouse, а сплит перестал бы быть
    воспроизводимым.
    """
    if not interactions:
        return TimeSplit(train=[], test=[], boundary_index=0)

    ordered = sorted(interactions, key=lambda item: (item.last_seen, item.user_id, item.film_id))
    boundary = int(len(ordered) * (1.0 - test_fraction))
    boundary = max(1, min(boundary, len(ordered)))
    return TimeSplit(train=ordered[:boundary], test=ordered[boundary:], boundary_index=boundary)


def group_by_user(interactions: list[Interaction]) -> dict[str, set[str]]:
    """Что посмотрел каждый пользователь — форма, в которой считаются метрики."""
    grouped: dict[str, set[str]] = {}
    for item in interactions:
        grouped.setdefault(item.user_id, set()).add(item.film_id)
    return grouped
