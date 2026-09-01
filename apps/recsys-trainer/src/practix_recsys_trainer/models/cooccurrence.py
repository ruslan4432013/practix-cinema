"""Со-встречаемость: «фильмы, которые смотрят одни и те же люди» (F2.2).

Близость — косинус между столбцами матрицы взаимодействий. Развёрнуто: два
фильма тем ближе, чем больше общих зрителей и чем меньше зрителей у каждого по
отдельности. Нормировка обязательна — без неё «похожим на всё» оказывался бы
самый популярный фильм каталога, потому что он пересекается со всеми.

ГРАНИЦА ПРИМЕНИМОСТИ ЗАФИКСИРОВАНА В ТЗ (раздел 6): матрица близости
квадратична по каталогу. На тысяче фильмов это миллион пар — секунды и
десятки мегабайт. На ста тысячах — 10^10 пар, и она не посчитается никогда.
Лечится усечением до top-K на этапе умножения (блоками по строкам, а не
целиком), и здесь сделан именно блочный проход: он не ускоряет тысячу фильмов,
но означает, что предел упирается в терпение, а не в MemoryError.

Матрица бинаризуется перед умножением. Вес просмотра (доля досмотра) хорош для
ALS, где он означает уверенность, но в со-встречаемости он смещал бы близость к
длинным фильмам: у них больше меток прогресса и выше шанс высокого веса.
Здесь важен факт «смотрел», а не «сколько».
"""

import numpy as np
from scipy import sparse

# Сколько столбцов обрабатывать за один проход. Ограничивает пиковую память
# произведением (block × films) float32: при 1024 и 10 000 фильмов — 40 МБ.
BLOCK_SIZE = 1024


def _binarize(matrix: sparse.csr_matrix) -> sparse.csr_matrix:
    binary = matrix.copy()
    binary.data = np.ones_like(binary.data, dtype=np.float32)
    return binary


def build_similar(
    matrix: sparse.csr_matrix,
    *,
    top_n: int,
    block_size: int = BLOCK_SIZE,
) -> dict[int, list[tuple[int, float]]]:
    """Для каждого фильма — до ``top_n`` ближайших, по убыванию близости.

    Возвращает индексы столбцов, а не идентификаторы: перевод в UUID делает
    вызывающий по карте из ``InteractionMatrix``. Так эта функция остаётся
    чистой арифметикой и проверяется юнит-тестом на матрице 4×4, посчитанной
    руками.
    """
    if matrix.shape[0] == 0 or matrix.shape[1] == 0:
        return {}

    binary = _binarize(matrix)
    # Косинус = (A^T A) с нормировкой на длины столбцов. Нормируем сами столбцы
    # ДО умножения: тогда произведение сразу даёт косинус, и не нужно хранить
    # вторую матрицу того же размера ради деления.
    norms = np.sqrt(np.asarray(binary.multiply(binary).sum(axis=0)).ravel())
    norms[norms == 0.0] = 1.0
    normalized = binary.multiply(sparse.csr_matrix(1.0 / norms)).tocsc().astype(np.float32)

    films = normalized.shape[1]
    result: dict[int, list[tuple[int, float]]] = {}

    for start in range(0, films, block_size):
        stop = min(start + block_size, films)
        # (block × users) @ (users × films) -> (block × films)
        block = (normalized[:, start:stop].T @ normalized).toarray()
        for offset in range(stop - start):
            film = start + offset
            scores = block[offset]
            # Фильм не может быть похож сам на себя (F2.3). Обнуляем ДО отбора
            # top-N, иначе он занял бы первую позицию у каждого фильма и
            # блок из десяти позиций отдавал бы девять полезных.
            scores[film] = 0.0
            neighbours = _top_n(scores, top_n)
            if neighbours:
                result[film] = neighbours

    return result


def _top_n(scores: np.ndarray, top_n: int) -> list[tuple[int, float]]:
    """Отбор ``top_n`` наибольших. Детерминированный, включая ничьи.

    ``argpartition`` за O(n) вместо O(n log n) у ``argsort``. На тысяче фильмов
    разница незаметна, на ста тысячах — это разница между минутами и часами,
    причём умноженная на число фильмов.

    Но САМ ОТБОР у него недетерминирован на границе: если на пороговой близости
    стоят три фильма, а мест осталось два, какие именно два попадут — зависит от
    внутреннего порядка partition. Отсортировать выбранное потом недостаточно:
    порядок исправится, а состав нет, и два прогона на одних и тех же данных
    дали бы разные витрины. Поэтому пороговое значение пересобирается явно:
    сначала всё, что строго выше порога, затем недостающее добирается с порога
    по возрастанию индекса. Оба ``flatnonzero`` идут по индексу, так что выбор
    воспроизводим по построению.
    """
    positive = int(np.count_nonzero(scores > 0.0))
    if positive == 0:
        return []

    take = min(top_n, positive)
    threshold = float(scores[np.argpartition(scores, -take)[-take:]].min())
    above = np.flatnonzero(scores > threshold)
    at_threshold = np.flatnonzero(scores == threshold)
    chosen = np.concatenate([above, at_threshold[: take - above.size]])

    picked = [(int(idx), float(scores[idx])) for idx in chosen]
    picked.sort(key=lambda item: (-item[1], item[0]))
    return picked
