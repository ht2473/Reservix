"""Методы многокритериального принятия решений: расчётное ядро без Django."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

#: Случайный индекс согласованности Саати: среднее значение индекса согласованности для случайно
#: заполненных обратно-симметричных матриц соответствующего порядка.
RANDOM_CONSISTENCY_INDEX: dict[int, float] = {
    1: 0.00,
    2: 0.00,
    3: 0.58,
    4: 0.90,
    5: 1.12,
    6: 1.24,
    7: 1.32,
    8: 1.41,
    9: 1.45,
    10: 1.49,
    11: 1.51,
    12: 1.48,
    13: 1.56,
    14: 1.57,
    15: 1.59,
}

#: Границы шкалы Саати.
SAATY_MIN = 1 / 9
SAATY_MAX = 9.0

#: Допуск проверки границ шкалы.
SAATY_TOLERANCE = 1e-3

#: Порог, ниже которого число считается нулём. Нужен там, где деление на
#: величину порядка машинной точности даёт бессмысленно большой результат.
EPSILON = 1e-12


class MCDAError(ValueError):
    """Ошибка исходных данных многокритериального расчёта."""


# ---------------------------------------------------------------------------
# Веса: метод анализа иерархий
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AHPResult:
    """Результат обработки матрицы парных сравнений."""

    #: Нормированные веса критериев в порядке строк матрицы, сумма равна единице.
    weights: tuple[float, ...]
    #: Наибольшее собственное значение матрицы.
    lambda_max: float
    #: Индекс согласованности CI = (λmax − n) / (n − 1).
    consistency_index: float
    #: Отношение согласованности CR = CI / RI.
    consistency_ratio: float
    #: Порядок матрицы.
    size: int

    def is_consistent(self, threshold: float = 0.10) -> bool:
        """Признаются ли суждения эксперта непротиворечивыми."""
        return self.consistency_ratio <= threshold


def validate_pairwise(matrix: Sequence[Sequence[float]]) -> np.ndarray:
    """Проверяет матрицу парных сравнений и возвращает её как массив."""
    array = np.asarray(matrix, dtype=float)
    if array.ndim != 2 or array.shape[0] != array.shape[1]:
        raise MCDAError("Матрица парных сравнений должна быть квадратной")
    if array.shape[0] == 0:
        raise MCDAError("Матрица парных сравнений пуста")
    if not np.all(np.isfinite(array)):
        raise MCDAError("Матрица содержит нечисловые значения")
    if np.any(array <= 0):
        raise MCDAError("Суждения шкалы Саати строго положительны")

    size = array.shape[0]
    if not np.allclose(np.diag(array), 1.0, atol=1e-6):
        raise MCDAError("На главной диагонали матрицы парных сравнений стоят единицы")

    tolerance = 1e-2
    for row in range(size):
        for column in range(row + 1, size):
            direct = array[row, column]
            inverse = array[column, row]
            if not (SAATY_MIN - SAATY_TOLERANCE <= direct <= SAATY_MAX + SAATY_TOLERANCE):
                raise MCDAError(f"Суждение ({row + 1}, {column + 1}) вне шкалы Саати: {direct:g}")
            if abs(direct * inverse - 1.0) > tolerance * max(1.0, direct * inverse):
                raise MCDAError(f"Нарушена обратная симметрия в паре ({row + 1}, {column + 1})")
    return array


def ahp_weights(matrix: Sequence[Sequence[float]]) -> AHPResult:
    """Вычисляет веса критериев методом анализа иерархий."""
    array = validate_pairwise(matrix)
    size = array.shape[0]

    if size == 1:
        return AHPResult(
            weights=(1.0,),
            lambda_max=1.0,
            consistency_index=0.0,
            consistency_ratio=0.0,
            size=1,
        )

    vector = np.full(size, 1.0 / size)
    for _iteration in range(100):
        updated = array @ vector
        total = float(updated.sum())
        if total <= EPSILON:  # pragma: no cover - исключено проверкой положительности
            raise MCDAError("Вырожденная матрица парных сравнений")
        updated = updated / total
        if float(np.abs(updated - vector).max()) < 1e-10:
            vector = updated
            break
        vector = updated

    # λmax = среднее отношение компонент Av к компонентам v.
    weighted = array @ vector
    lambda_max = float(np.mean(weighted / vector))

    consistency_index = (lambda_max - size) / (size - 1)
    random_index = RANDOM_CONSISTENCY_INDEX.get(size, 1.59)
    consistency_ratio = 0.0 if random_index <= 0 else consistency_index / random_index

    return AHPResult(
        weights=tuple(float(value) for value in vector),
        lambda_max=lambda_max,
        # Отрицательные значения порядка машинной точности возникают на идеально согласованных
        # матрицах и означают ноль, а не отрицательную противоречивость: показывать «CI =
        # −0,000001» бессмысленно.
        consistency_index=max(0.0, consistency_index),
        consistency_ratio=max(0.0, consistency_ratio),
        size=size,
    )


def pairwise_from_judgements(
    size: int, judgements: dict[tuple[int, int], float]
) -> list[list[float]]:
    """Собирает полную матрицу парных сравнений из суждений верхнего треугольника."""
    if size <= 0:
        raise MCDAError("Число критериев должно быть положительным")

    matrix = [[1.0] * size for _ in range(size)]
    for (row, column), value in judgements.items():
        if not 0 <= row < size or not 0 <= column < size:
            raise MCDAError(f"Пара ({row}, {column}) выходит за границы матрицы")
        if row == column:
            continue
        if value <= 0:
            raise MCDAError("Суждение шкалы Саати строго положительно")
        matrix[row][column] = float(value)
        matrix[column][row] = 1.0 / float(value)
    return matrix


# ---------------------------------------------------------------------------
# Веса: энтропийный метод
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class EntropyResult:
    """Результат расчёта весов энтропийным методом."""

    weights: tuple[float, ...]
    entropies: tuple[float, ...]
    divergences: tuple[float, ...]
    #: Число альтернатив, по которым получены веса. Веса, выведенные из выборки
    #: в три кандидата, недостоверны, и знаменатель обязан быть виден.
    sample_size: int
    #: Признак вырожденного случая: ни один критерий не различает альтернативы,
    #: и веса назначены равными за отсутствием оснований для различия.
    is_degenerate: bool = False


def entropy_weights(matrix: Sequence[Sequence[float]]) -> EntropyResult:
    """Вычисляет веса критериев энтропийным методом Шеннона."""
    array = _as_matrix(matrix)
    alternatives, criteria = array.shape

    if alternatives < 2:
        raise MCDAError("Энтропийный метод требует не менее двух альтернатив")
    if np.any(array < 0):
        raise MCDAError("Энтропийный метод определён на неотрицательных значениях")

    # Сдвиг на малую величину избавляет от нулевых долей: ln(0) не определён, а столбец, где часть
    # кандидатов имеет ноль, — обычное дело (мероприятия плана развития не начаты, оценка 360° не
    # проводилась).
    shifted = array + EPSILON
    column_sums = shifted.sum(axis=0)
    proportions = shifted / column_sums

    scale = 1.0 / math.log(alternatives)
    entropies = -scale * np.sum(proportions * np.log(proportions), axis=0)
    entropies = np.clip(entropies, 0.0, 1.0)
    divergences = 1.0 - entropies

    total_divergence = float(divergences.sum())
    if total_divergence <= EPSILON:
        equal = 1.0 / criteria
        return EntropyResult(
            weights=tuple([equal] * criteria),
            entropies=tuple(float(value) for value in entropies),
            divergences=tuple(float(value) for value in divergences),
            sample_size=alternatives,
            is_degenerate=True,
        )

    weights = divergences / total_divergence
    return EntropyResult(
        weights=tuple(float(value) for value in weights),
        entropies=tuple(float(value) for value in entropies),
        divergences=tuple(float(value) for value in divergences),
        sample_size=alternatives,
    )


# ---------------------------------------------------------------------------
# Свёртка
# ---------------------------------------------------------------------------
def weighted_sum(matrix: Sequence[Sequence[float]], weights: Sequence[float]) -> tuple[float, ...]:
    """Взвешенная сумма частных оценок."""
    array = _as_matrix(matrix)
    vector = _as_weights(weights, array.shape[1])
    return tuple(float(value) for value in array @ vector)


def weighted_geometric(
    matrix: Sequence[Sequence[float]], weights: Sequence[float]
) -> tuple[float, ...]:
    """Взвешенное геометрическое среднее частных оценок."""
    array = _as_matrix(matrix)
    vector = _as_weights(weights, array.shape[1])
    safe = np.clip(array, EPSILON, None)
    return tuple(float(value) for value in np.exp(np.log(safe) @ vector))


@dataclass(frozen=True, slots=True)
class TopsisResult:
    """Результат ранжирования методом TOPSIS."""

    #: Коэффициент близости к идеальному решению в шкале 0–100, по одному на
    #: альтернативу в порядке строк исходной матрицы.
    closeness: tuple[float, ...]
    #: Расстояния до идеального и антиидеального решений.
    distance_best: tuple[float, ...]
    distance_worst: tuple[float, ...]
    #: Координаты идеального и антиидеального решений во взвешенной шкале.
    ideal_best: tuple[float, ...]
    ideal_worst: tuple[float, ...]


def topsis(
    matrix: Sequence[Sequence[float]],
    weights: Sequence[float],
    directions: Sequence[bool] | None = None,
) -> TopsisResult:
    """Ранжирует альтернативы методом TOPSIS."""
    array = _as_matrix(matrix)
    criteria = array.shape[1]
    vector = _as_weights(weights, criteria)

    benefit = (
        np.ones(criteria, dtype=bool)
        if directions is None
        else np.asarray(list(directions), dtype=bool)
    )
    if benefit.shape[0] != criteria:
        raise MCDAError("Число направлений не совпадает с числом критериев")

    # Векторная нормировка.
    norms = np.sqrt((array**2).sum(axis=0))
    norms = np.where(norms <= EPSILON, 1.0, norms)
    weighted = (array / norms) * vector

    column_max = weighted.max(axis=0)
    column_min = weighted.min(axis=0)
    ideal_best = np.where(benefit, column_max, column_min)
    ideal_worst = np.where(benefit, column_min, column_max)

    distance_best = np.sqrt(((weighted - ideal_best) ** 2).sum(axis=1))
    distance_worst = np.sqrt(((weighted - ideal_worst) ** 2).sum(axis=1))

    total = distance_best + distance_worst
    # Совпадение идеального и антиидеального решений означает, что все альтернативы одинаковы.
    safe_total = np.where(total <= EPSILON, 1.0, total)
    closeness = np.where(total <= EPSILON, 0.5, distance_worst / safe_total)

    return TopsisResult(
        closeness=tuple(float(value * 100) for value in closeness),
        distance_best=tuple(float(value) for value in distance_best),
        distance_worst=tuple(float(value) for value in distance_worst),
        ideal_best=tuple(float(value) for value in ideal_best),
        ideal_worst=tuple(float(value) for value in ideal_worst),
    )


# ---------------------------------------------------------------------------
# Ранги и согласие ранжирований
# ---------------------------------------------------------------------------
def ranks(scores: Sequence[float], *, descending: bool = True) -> tuple[float, ...]:
    """Переводит оценки в ранги, разделяя ранг между совпадающими значениями."""
    array = np.asarray(list(scores), dtype=float)
    if array.size == 0:
        return ()

    order = -array if descending else array
    sorted_index = np.argsort(order, kind="stable")
    result = np.empty(array.size, dtype=float)

    position = 0
    while position < array.size:
        end = position
        while (
            end + 1 < array.size and order[sorted_index[end + 1]] == order[sorted_index[position]]
        ):
            end += 1
        average_rank = (position + end) / 2 + 1
        for index in sorted_index[position : end + 1]:
            result[index] = average_rank
        position = end + 1

    return tuple(float(value) for value in result)


def spearman(first: Sequence[float], second: Sequence[float]) -> float:
    """Коэффициент ранговой корреляции Спирмена между двумя наборами оценок."""
    if len(first) != len(second):
        raise MCDAError("Сравниваемые ранжирования разной длины")
    if len(first) < 2:
        return 0.0

    first_ranks = np.asarray(ranks(first), dtype=float)
    second_ranks = np.asarray(ranks(second), dtype=float)

    if float(first_ranks.std()) <= EPSILON or float(second_ranks.std()) <= EPSILON:
        return 0.0

    correlation = float(np.corrcoef(first_ranks, second_ranks)[0, 1])
    return round(correlation, 4)


def rank_agreement(first: Sequence[float], second: Sequence[float], top: int = 10) -> float:
    """Доля совпадения состава лучших ``top`` позиций двух ранжирований."""
    if not first or len(first) != len(second):
        return 0.0

    size = min(top, len(first))
    first_top = set(np.argsort(-np.asarray(list(first), dtype=float), kind="stable")[:size])
    second_top = set(np.argsort(-np.asarray(list(second), dtype=float), kind="stable")[:size])
    return round(len(first_top & second_top) / size, 4)


# ---------------------------------------------------------------------------
# Анализ устойчивости
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PerturbationOutcome:
    """Результат одного возмущения весов."""

    #: Относительная величина возмущения, доли единицы (0,1 — десять процентов).
    delta: float
    #: Индекс критерия, вес которого изменён; ``None`` — возмущены все веса.
    criterion_index: int | None
    #: Корреляция Спирмена между исходным и возмущённым ранжированием.
    correlation: float
    #: Доля альтернатив, сохранивших свой ранг в точности.
    stable_share: float
    #: Доля совпадения состава первой десятки.
    top_agreement: float
    #: Наибольшее смещение ранга по всем альтернативам.
    max_rank_shift: float


@dataclass(slots=True)
class SensitivityReport:
    """Сводка анализа устойчивости ранжирования."""

    outcomes: list[PerturbationOutcome] = field(default_factory=list)
    #: Минимальная корреляция по всем возмущениям — характеристика худшего случая.
    worst_correlation: float = 1.0
    #: Средняя доля альтернатив, сохранивших ранг.
    average_stability: float = 1.0
    #: Наименее устойчивый критерий: возмущение его веса сильнее прочих меняет
    #: порядок кандидатов, то есть именно его обоснование требует внимания.
    critical_criterion: int | None = None

    @property
    def is_robust(self) -> bool:
        """Признаётся ли ранжирование устойчивым по принятому порогу 0,9."""
        return self.worst_correlation >= 0.9


def perturb_weights(weights: Sequence[float], index: int, delta: float) -> tuple[float, ...]:
    """Изменяет вес одного критерия на заданную долю с перенормировкой остальных."""
    vector = np.asarray(list(weights), dtype=float)
    if not 0 <= index < vector.size:
        raise MCDAError(f"Критерий {index} отсутствует в наборе весов")

    changed = max(0.0, vector[index] * (1.0 + delta))
    remainder = float(vector.sum() - vector[index])

    result = vector.copy()
    result[index] = changed
    if remainder > EPSILON:
        # Оставшаяся масса распределяется между прочими критериями
        # пропорционально их прежним весам.
        scale = (max(0.0, vector.sum() - changed)) / remainder
        for position in range(vector.size):
            if position != index:
                result[position] = vector[position] * scale

    total = float(result.sum())
    if total <= EPSILON:  # pragma: no cover - возможно лишь при нулевых весах
        return tuple([1.0 / vector.size] * vector.size)
    return tuple(float(value) for value in result / total)


def sensitivity(
    matrix: Sequence[Sequence[float]],
    weights: Sequence[float],
    *,
    deltas: Sequence[float] = (0.10, 0.20, 0.30),
    aggregate: str = "weighted_sum",
    top: int = 10,
) -> SensitivityReport:
    """Проверяет устойчивость ранжирования к возмущению весов критериев."""
    array = _as_matrix(matrix)
    vector = _as_weights(weights, array.shape[1])
    if array.shape[0] < 2:
        return SensitivityReport()

    baseline = _aggregate(array, vector, aggregate)
    baseline_ranks = ranks(baseline)

    report = SensitivityReport(outcomes=[])
    worst = 1.0
    stability_sum = 0.0
    per_criterion: dict[int, float] = {}

    for criterion in range(array.shape[1]):
        for magnitude in deltas:
            for sign in (1.0, -1.0):
                delta = sign * magnitude
                shifted = perturb_weights(vector.tolist(), criterion, delta)
                scores = _aggregate(array, np.asarray(shifted, dtype=float), aggregate)
                shifted_ranks = ranks(scores)

                correlation = spearman(baseline, scores)
                stable = sum(
                    1
                    for original, moved in zip(baseline_ranks, shifted_ranks, strict=True)
                    if original == moved
                )
                stable_share = round(stable / len(baseline_ranks), 4)
                shift = max(
                    abs(original - moved)
                    for original, moved in zip(baseline_ranks, shifted_ranks, strict=True)
                )

                report.outcomes.append(
                    PerturbationOutcome(
                        delta=delta,
                        criterion_index=criterion,
                        correlation=correlation,
                        stable_share=stable_share,
                        top_agreement=rank_agreement(baseline, scores, top=top),
                        max_rank_shift=round(shift, 2),
                    )
                )
                worst = min(worst, correlation)
                stability_sum += stable_share
                per_criterion[criterion] = min(per_criterion.get(criterion, 1.0), correlation)

    count = len(report.outcomes)
    report.worst_correlation = round(worst, 4)
    report.average_stability = round(stability_sum / count, 4) if count else 1.0
    report.critical_criterion = (
        min(per_criterion, key=lambda key: per_criterion[key]) if per_criterion else None
    )
    return report


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------
def _aggregate(matrix: np.ndarray, weights: np.ndarray, method: str) -> tuple[float, ...]:
    """Применяет названную схему свёртки к матрице решений."""
    if method == "topsis":
        return topsis(matrix.tolist(), weights.tolist()).closeness
    if method == "geometric":
        return weighted_geometric(matrix.tolist(), weights.tolist())
    return weighted_sum(matrix.tolist(), weights.tolist())


def _as_matrix(matrix: Sequence[Sequence[float]]) -> np.ndarray:
    """Приводит матрицу решений к массиву с проверкой формы."""
    array = np.asarray([list(row) for row in matrix], dtype=float)
    if array.ndim != 2 or array.size == 0:
        raise MCDAError("Матрица решений должна быть непустой прямоугольной таблицей")
    if not np.all(np.isfinite(array)):
        raise MCDAError("Матрица решений содержит нечисловые значения")
    return array


def _as_weights(weights: Sequence[float], expected: int) -> np.ndarray:
    """Приводит веса к массиву, нормируя их сумму к единице."""
    vector = np.asarray(list(weights), dtype=float)
    if vector.size != expected:
        raise MCDAError(f"Ожидалось {expected} весов, получено {vector.size}")
    if np.any(vector < 0):
        raise MCDAError("Вес критерия не может быть отрицательным")

    total = float(vector.sum())
    if total <= EPSILON:
        return np.full(expected, 1.0 / expected)
    return vector / total
