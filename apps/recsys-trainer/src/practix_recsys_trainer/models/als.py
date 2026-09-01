"""Персональные рекомендации: матричная факторизация ALS (E6, ADR-007).

Почему ALS, а не нейросеть. Двухбашенная сеть не укладывается в бюджет эпика,
который к тому же режется первым при отставании: обучение плюс подбор
гиперпараметров — это недели, а не пять дней. Item2vec отвергнут по другой
причине — он даёт похожесть, то есть дублирует со-встречаемость, а не
персональную выдачу.

Почему библиотека, а не сервис. Готовый движок (Gorse) принёс бы своё
хранилище и свою модель данных, а за швом витрины модель не видна никому:
`implicit` можно выбросить, не тронув ни API, ни nginx, ни фронт. Именно это
свойство и позволяет резать E6 целиком, не переделывая остальное.

ALPHA — ЭТО НЕ ПРОСТО МНОЖИТЕЛЬ. В формулировке Hu-Koren-Volinsky для неявного
сигнала матрица означает не «оценку», а «уверенность»: c = 1 + alpha·r. Ноль в
матрице — это «не знаю», а не «не нравится», и alpha задаёт, насколько сильнее
мы верим наблюдению, чем пустоте. Передать сюда сырые доли просмотра без alpha
значило бы почти уравнять просмотр с отсутствием просмотра.
"""

import logging

import numpy as np
from scipy import sparse

logger = logging.getLogger(__name__)


def train_and_recommend(
    matrix: sparse.csr_matrix,
    *,
    factors: int,
    iterations: int,
    regularization: float,
    alpha: float,
    top_n: int,
    seed: int,
    max_users: int,
) -> dict[int, list[tuple[int, float]]]:
    """Обучает ALS и возвращает по ``top_n`` позиций на пользователя.

    Ключ и значения — индексы матрицы, перевод в UUID делает вызывающий.

    УЖЕ ПРОСМОТРЕННОЕ ИСКЛЮЧАЕТСЯ ЗДЕСЬ, А НЕ БИБЛИОТЕКОЙ. Флаг
    ``filter_already_liked_items`` выставлен, но полагаться на него нельзя:
    когда неотфильтрованных кандидатов меньше запрошенного ``N``, implicit
    ДОБИВАЕТ выдачу теми самыми просмотренными позициями. Проверено отдельно на
    матрице 24×12: у пользователя, посмотревшего 6 фильмов из 12, запрос десяти
    рекомендаций вернул 6 новых и 4 уже просмотренных.

    В бою это бьёт ровно по активным зрителям — по тем, кто посмотрел заметную
    долю каталога, — то есть по самой ценной аудитории. А F4.3 не знает слова
    «обычно»: просмотренного в выдаче быть не должно. Поэтому строка
    пользователя перепроверяется явно, и выдача усекается ПОСЛЕ фильтра.
    """
    users, films = matrix.shape
    if users == 0 or films == 0:
        return {}

    # Импорт внутри функции намеренно: implicit тянет за собой BLAS-потоки и
    # заметное время импорта, а команды `stats`, `generate` и `dataset` в нём
    # не нуждаются вовсе. Платить за него на каждом запуске CLI незачем.
    from implicit.als import AlternatingLeastSquares

    # implicit сам параллелит через OpenMP и ругается, если BLAS тоже
    # многопоточен: два уровня параллелизма на одной матрице дают не ускорение,
    # а конкуренцию за ядра.
    model = AlternatingLeastSquares(
        factors=min(factors, max(2, min(users, films) - 1)),
        iterations=iterations,
        regularization=regularization,
        alpha=alpha,
        random_state=seed,
        use_gpu=False,
    )
    model.fit(matrix, show_progress=False)

    # Пользователи берутся самые активные, а не первые попавшиеся: ограничение
    # RECS_TRAINER_MAX_PERSONAL_USERS существует ради размера витрины, и если
    # его придётся применить, отрезать надо хвост, а не случайную половину.
    if users > max_users:
        activity = np.asarray((matrix > 0).sum(axis=1)).ravel()
        selected = np.argsort(-activity, kind='stable')[:max_users].astype(np.int32)
    else:
        selected = np.arange(users, dtype=np.int32)

    # Просим с запасом: часть кандидатов отсеется собственным фильтром ниже, и
    # без запаса выдача у активных зрителей оказалась бы короче top_n.
    requested = min(films, top_n * 2)
    ids, scores = model.recommend(
        selected,
        matrix[selected],
        N=requested,
        filter_already_liked_items=True,
    )

    result: dict[int, list[tuple[int, float]]] = {}
    for row, user in enumerate(selected):
        watched = set(matrix.indices[matrix.indptr[user] : matrix.indptr[user + 1]].tolist())
        positions: list[tuple[int, float]] = []
        for film, score in zip(ids[row], scores[row], strict=True):
            # implicit добивает выдачу -1, когда кандидатов не хватает вовсе.
            if film < 0 or not np.isfinite(score) or int(film) in watched:
                continue
            positions.append((int(film), float(score)))
            if len(positions) >= top_n:
                break
        if positions:
            result[int(user)] = positions
    return result
