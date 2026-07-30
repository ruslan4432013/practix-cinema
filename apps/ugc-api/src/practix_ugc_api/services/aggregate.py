"""Арифметика преагрегата: дельты и чтение гистограммы.

Вынесено отдельно от сервисов, потому что это единственная часть записи, которую
можно проверить без базы, — и единственная, где легко ошибиться. Каноническая
ошибка: при ИЗМЕНЕНИИ оценки менять счётчик количества (он не меняется) или
слепо делать ``+1/-1`` по двум позициям гистограммы, когда новая оценка совпала
со старой и не меняется вообще ничего.

Функции перенесены из ``research/ugc-storage/lib/store.py::rating_deltas`` и
расширены четвёртым случаем — удалением оценки, которого в стенде не было.
"""

from dataclasses import dataclass

# Оценки 0..10 включительно — одиннадцать позиций.
HISTOGRAM_SIZE = 11


@dataclass(frozen=True)
class RatingDelta:
    """Сдвиг преагрегата фильма при постановке, изменении или снятии оценки."""

    count: int
    total: int
    histogram: list[int]

    @property
    def is_noop(self) -> bool:
        """Нечего записывать: оценка не изменилась."""
        return self.count == 0 and self.total == 0 and not any(self.histogram)


def rating_deltas(old_rating: int | None, new_rating: int | None) -> RatingDelta:
    """Дельта преагрегата при переходе оценки из ``old_rating`` в ``new_rating``.

    ``None`` означает отсутствие оценки, поэтому одна функция закрывает все
    четыре случая: постановка (None → n), изменение (m → n), повтор той же
    оценки (n → n) и снятие (n → None).
    """
    histogram = [0] * HISTOGRAM_SIZE
    if old_rating == new_rating:
        return RatingDelta(count=0, total=0, histogram=histogram)

    count = 0
    total = 0
    if old_rating is not None:
        count -= 1
        total -= old_rating
        histogram[old_rating] -= 1
    if new_rating is not None:
        count += 1
        total += new_rating
        histogram[new_rating] += 1
    return RatingDelta(count=count, total=total, histogram=histogram)


def split_by_threshold(histogram: list[int], threshold: int) -> tuple[int, int]:
    """Разбивает гистограмму на «лайки» и «дизлайки» по порогу.

    Оценка считается лайком начиная с ``threshold`` включительно. Порог —
    аргумент, а не константа: ровно ради возможности его менять хранится
    гистограмма, а не пара счётчиков.
    """
    return sum(histogram[threshold:]), sum(histogram[:threshold])


def average(ratings_sum: int, ratings_count: int) -> float | None:
    """Средняя оценка; ``None``, если фильм ещё никто не оценивал.

    Ноль здесь был бы враньём: «средняя оценка 0» и «оценок нет» — разные вещи,
    и в выдаче они обязаны отличаться.
    """
    if not ratings_count:
        return None
    return ratings_sum / ratings_count


def vote_deltas(old_value: int | None, new_value: int | None) -> tuple[int, int, int]:
    """Дельты ``(votes_likes, votes_dislikes, useful_score)`` при смене голоса.

    Как и с оценками, ``None`` — отсутствие голоса, поэтому постановка, смена и
    снятие считаются одной формулой.
    """
    likes = int(new_value == 1) - int(old_value == 1)
    dislikes = int(new_value == -1) - int(old_value == -1)
    return likes, dislikes, likes - dislikes
