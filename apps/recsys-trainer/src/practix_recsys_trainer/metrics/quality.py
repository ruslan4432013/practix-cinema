"""Метрики качества рекомендаций (E5): precision@k, recall@k, покрытие.

Зачем это вообще есть. Без измерения вопрос «откуда вы знаете, что
рекомендации хорошие» закрыть нечем, а сравнивать модель саму с собой
бессмысленно. Поэтому BASELINE «просто популярное» фиксируется первым и
считается тем же кодом на той же выборке (F3.2): любая модель обязана его
обогнать, иначе она не заработала своей сложности.

Отрицательный результат тоже результат — E5 стоит в плане ДО E6 намеренно.

Всё здесь — чистые функции без ввода-вывода. Это не стилистика: арифметику
метрик легко испортить незаметно (делить на k вместо |relevant|, считать
покрытие по выдаче вместо каталога), и единственная защита — проверяемость
примерами, посчитанными руками.
"""

from dataclasses import dataclass

# Средние по пользователям, а не по всем предсказаниям («macro», не «micro»).
# Микро-усреднение отдало бы вес самым активным зрителям, и метрика измеряла бы
# качество для сотни киноманов вместо качества для аудитории.


@dataclass(frozen=True, slots=True)
class QualityReport:
    """Результат оценки одной модели на одной отложенной выборке."""

    name: str
    k: int
    precision: float
    recall: float
    coverage: float
    users_evaluated: int

    def as_row(self) -> dict[str, float | str | int]:
        return {
            'model': self.name,
            'k': self.k,
            'precision@k': round(self.precision, 4),
            'recall@k': round(self.recall, 4),
            'coverage': round(self.coverage, 4),
            'users': self.users_evaluated,
        }


def precision_at_k(recommended: list[str], relevant: set[str], k: int) -> float:
    """Какая доля выданных позиций оказалась угадана.

    Знаменатель — ``k``, а НЕ длина выдачи. Иначе модель, вернувшая одну
    позицию и угадавшая её, получила бы precision@10 = 1.0 и обошла бы модель,
    вернувшую десять позиций и угадавшую пять.
    """
    if k <= 0:
        return 0.0
    top = recommended[:k]
    return len([item for item in top if item in relevant]) / k


def recall_at_k(recommended: list[str], relevant: set[str], k: int) -> float:
    """Какую долю того, что человек реально посмотрел, мы предложили.

    Знаменатель — размер релевантного множества. Пользователи без релевантных
    позиций из усреднения исключаются вызывающим, а не считаются нулями: ноль
    здесь означал бы «модель промахнулась», хотя промахиваться было не по чему.
    """
    if not relevant:
        return 0.0
    top = recommended[:k]
    return len([item for item in top if item in relevant]) / len(relevant)


def catalog_coverage(recommended_per_user: dict[str, list[str]], catalog_size: int) -> float:
    """Какая доля КАТАЛОГА вообще попадает хоть в чью-то выдачу.

    Знаменатель — размер каталога, а не число уникальных фильмов в выдаче:
    покрытие ровно про то, вытаскиваем ли мы длинный хвост, ради которого
    рекомендации и затевались. Метрика, посчитанная по выдаче, всегда равнялась
    бы единице и не значила бы ничего.
    """
    if catalog_size <= 0:
        return 0.0
    shown = {film for films in recommended_per_user.values() for film in films}
    return len(shown) / catalog_size


def evaluate(
    name: str,
    recommended_per_user: dict[str, list[str]],
    relevant_per_user: dict[str, set[str]],
    *,
    k: int,
    catalog_size: int,
) -> QualityReport:
    """Сводка по модели. Оцениваются только пользователи, у которых есть и то и другое."""
    precisions: list[float] = []
    recalls: list[float] = []
    evaluated: dict[str, list[str]] = {}

    for user_id, relevant in relevant_per_user.items():
        if not relevant:
            continue
        recommended = recommended_per_user.get(user_id)
        if not recommended:
            # Модель ничего не предложила этому человеку — это промах, и он
            # обязан попасть в среднее. Пропустить такие случаи значило бы
            # мерить качество только там, где модель и так сработала.
            precisions.append(0.0)
            recalls.append(0.0)
            continue
        precisions.append(precision_at_k(recommended, relevant, k))
        recalls.append(recall_at_k(recommended, relevant, k))
        evaluated[user_id] = recommended[:k]

    if not precisions:
        return QualityReport(name=name, k=k, precision=0.0, recall=0.0, coverage=0.0, users_evaluated=0)

    return QualityReport(
        name=name,
        k=k,
        precision=sum(precisions) / len(precisions),
        recall=sum(recalls) / len(recalls),
        coverage=catalog_coverage(evaluated, catalog_size),
        users_evaluated=len(precisions),
    )


def format_table(reports: list[QualityReport]) -> str:
    """Таблица сравнения в текст — то, что печатает ``evaluate`` (F3.3).

    Своя разметка, а не сторонняя библиотека: одна таблица из четырёх колонок
    не стоит зависимости, которую придётся тащить в образ.
    """
    headers = ['модель', 'k', 'precision@k', 'recall@k', 'покрытие', 'польз.']
    rows = [
        [
            report.name,
            str(report.k),
            f'{report.precision:.4f}',
            f'{report.recall:.4f}',
            f'{report.coverage:.4f}',
            str(report.users_evaluated),
        ]
        for report in reports
    ]
    widths = [
        max(len(header), *(len(row[idx]) for row in rows)) if rows else len(header)
        for idx, header in enumerate(headers)
    ]
    line = '-+-'.join('-' * width for width in widths)
    out = [' | '.join(header.ljust(widths[idx]) for idx, header in enumerate(headers)), line]
    out.extend(' | '.join(cell.ljust(widths[idx]) for idx, cell in enumerate(row)) for row in rows)
    return '\n'.join(out)
