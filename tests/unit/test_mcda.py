"""Проверка расчётного ядра многокритериальных методов."""

from __future__ import annotations

import pytest

from apps.analytics.mcda import (
    MCDAError,
    ahp_weights,
    entropy_weights,
    pairwise_from_judgements,
    perturb_weights,
    rank_agreement,
    ranks,
    sensitivity,
    spearman,
    topsis,
    validate_pairwise,
    weighted_geometric,
    weighted_sum,
)

pytestmark = [pytest.mark.unit]


# ---------------------------------------------------------------------------
# Метод анализа иерархий
# ---------------------------------------------------------------------------
class TestAHP:
    """Веса и согласованность матрицы парных сравнений."""

    #: Классический пример Саати порядка 3.
    SAATY_EXAMPLE = [[1, 3, 5], [1 / 3, 1, 3], [1 / 5, 1 / 3, 1]]

    def test_matches_reference_example(self):
        """Веса совпадают с опубликованным решением примера Саати."""
        result = ahp_weights(self.SAATY_EXAMPLE)

        assert result.weights[0] == pytest.approx(0.637, abs=0.001)
        assert result.weights[1] == pytest.approx(0.258, abs=0.001)
        assert result.weights[2] == pytest.approx(0.105, abs=0.001)
        assert result.lambda_max == pytest.approx(3.039, abs=0.001)
        assert result.consistency_ratio == pytest.approx(0.033, abs=0.002)

    def test_weights_form_distribution(self):
        """Сумма весов равна единице: иначе это не распределение."""
        result = ahp_weights(self.SAATY_EXAMPLE)

        assert sum(result.weights) == pytest.approx(1.0)

    def test_perfectly_consistent_matrix(self):
        """Согласованная матрица даёт нулевое отношение согласованности."""
        result = ahp_weights([[1, 2, 4], [0.5, 1, 2], [0.25, 0.5, 1]])

        assert result.consistency_ratio == pytest.approx(0.0, abs=1e-9)
        assert result.is_consistent()
        assert result.weights == pytest.approx((4 / 7, 2 / 7, 1 / 7), abs=1e-6)

    def test_contradictory_judgements_exceed_threshold(self):
        """Круговое предпочтение признаётся противоречивым."""
        matrix = pairwise_from_judgements(3, {(0, 1): 9, (1, 2): 9, (0, 2): 1 / 9})

        result = ahp_weights(matrix)

        assert result.consistency_ratio > 0.1
        assert not result.is_consistent()

    def test_single_criterion(self):
        """Единственный критерий получает весь вес и согласован тождественно."""
        result = ahp_weights([[1]])

        assert result.weights == (1.0,)
        assert result.consistency_ratio == 0.0

    def test_rounded_reciprocal_is_accepted(self):
        """Округлённое обратное суждение проходит проверку шкалы."""
        matrix = pairwise_from_judgements(2, {(0, 1): 0.1111})

        result = ahp_weights(matrix)

        assert result.weights[1] > result.weights[0]

    @pytest.mark.parametrize(
        ("matrix", "reason"),
        [
            ([[1, 2], [3, 1]], "нарушена обратная симметрия"),
            ([[2, 1], [1, 2]], "на диагонали не единицы"),
            ([[1, 2, 3], [0.5, 1, 2]], "матрица не квадратная"),
            ([[1, 12], [1 / 12, 1]], "значение вне шкалы Саати"),
            ([[1, 0], [0, 1]], "неположительное суждение"),
        ],
    )
    def test_rejects_invalid_matrix(self, matrix, reason):
        """Матрица, не отвечающая условиям шкалы, к расчёту не принимается."""
        with pytest.raises(MCDAError):
            validate_pairwise(matrix)

    def test_judgements_fill_lower_triangle(self):
        """Обратные суждения достраиваются делением, а не вводятся заново."""
        matrix = pairwise_from_judgements(3, {(0, 1): 3, (1, 2): 2})

        assert matrix[1][0] == pytest.approx(1 / 3)
        assert matrix[2][1] == pytest.approx(1 / 2)
        assert matrix[0][2] == 1.0


# ---------------------------------------------------------------------------
# Свёртка
# ---------------------------------------------------------------------------
class TestAggregation:
    """Взвешенная сумма, геометрическое среднее и TOPSIS."""

    #: Контрольный пример TOPSIS: четыре критерия, первый — «чем меньше, тем лучше».
    TOPSIS_MATRIX = [
        [250, 16, 12, 5],
        [200, 16, 8, 3],
        [300, 32, 16, 4],
        [275, 32, 8, 4],
        [225, 16, 16, 2],
    ]

    def test_topsis_matches_reference_example(self):
        """Коэффициенты близости совпадают с контрольным примером."""
        result = topsis(self.TOPSIS_MATRIX, [0.25] * 4, [False, True, True, True])

        expected = (53.43, 30.84, 69.16, 53.47, 40.10)
        assert result.closeness == pytest.approx(expected, abs=0.05)

    def test_weighted_sum_is_absolute(self):
        """Взвешенная сумма не зависит от состава сравниваемой группы."""
        alone = weighted_sum([[80, 60]], [0.5, 0.5])
        in_group = weighted_sum([[80, 60], [100, 100], [10, 10]], [0.5, 0.5])

        assert alone[0] == pytest.approx(70.0)
        assert in_group[0] == pytest.approx(70.0)

    def test_topsis_is_relative(self):
        """Коэффициент близости зависит от состава группы."""
        pair = topsis([[80, 60], [40, 40]], [0.5, 0.5]).closeness
        triple = topsis([[80, 60], [40, 40], [100, 100]], [0.5, 0.5]).closeness

        assert pair[0] != pytest.approx(triple[0], abs=0.5)

    def test_identical_alternatives_get_middle(self):
        """Совпадение всех альтернатив даёт половину шкалы."""
        result = topsis([[50, 50], [50, 50]], [0.5, 0.5])

        assert result.closeness == pytest.approx((50.0, 50.0))

    def test_geometric_penalises_weak_criterion(self):
        """Геометрическое среднее наказывает провал сильнее линейной свёртки."""
        balanced = weighted_geometric([[60, 60]], [0.5, 0.5])[0]
        skewed = weighted_geometric([[10, 110]], [0.5, 0.5])[0]
        linear_skewed = weighted_sum([[10, 110]], [0.5, 0.5])[0]

        assert skewed < balanced
        assert skewed < linear_skewed

    def test_zero_weights_fall_back_to_equal(self):
        """Нулевые веса не приводят к делению на ноль."""
        result = weighted_sum([[80, 40]], [0, 0])

        assert result[0] == pytest.approx(60.0)

    def test_rejects_weight_count_mismatch(self):
        """Число весов обязано совпадать с числом критериев."""
        with pytest.raises(MCDAError):
            weighted_sum([[80, 40]], [0.5, 0.3, 0.2])


# ---------------------------------------------------------------------------
# Энтропийные веса
# ---------------------------------------------------------------------------
class TestEntropy:
    """Веса, выведенные из разброса значений."""

    def test_indiscriminate_criterion_gets_zero_weight(self):
        """Критерий с одинаковыми значениями получает нулевой вес."""
        result = entropy_weights([[10, 50], [30, 50], [90, 50]])

        assert result.weights[1] == pytest.approx(0.0, abs=1e-6)
        assert result.weights[0] == pytest.approx(1.0, abs=1e-6)
        assert result.entropies[1] == pytest.approx(1.0, abs=1e-6)

    def test_weights_form_distribution(self):
        """Сумма весов равна единице."""
        result = entropy_weights([[10, 20, 30], [20, 20, 10], [30, 20, 50]])

        assert sum(result.weights) == pytest.approx(1.0)

    def test_uniform_matrix_is_degenerate(self):
        """Полностью однородная матрица даёт равные веса и признак вырожденности."""
        result = entropy_weights([[50, 50], [50, 50], [50, 50]])

        assert result.is_degenerate
        assert result.weights == pytest.approx((0.5, 0.5))

    def test_requires_two_alternatives(self):
        """Разброс по одной альтернативе не определён."""
        with pytest.raises(MCDAError):
            entropy_weights([[10, 20]])

    def test_rejects_negative_values(self):
        """Метод определён на неотрицательных значениях."""
        with pytest.raises(MCDAError):
            entropy_weights([[10, -5], [20, 30]])


# ---------------------------------------------------------------------------
# Ранги и согласие ранжирований
# ---------------------------------------------------------------------------
class TestRanks:
    """Ранжирование и его сравнение."""

    def test_ties_share_average_rank(self):
        """Совпавшие оценки делят ранг поровну."""
        assert ranks([10, 20, 20, 5]) == (3.0, 1.5, 1.5, 4.0)

    def test_spearman_detects_full_agreement(self):
        """Одинаковый порядок даёт единицу, обратный — минус единицу."""
        assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
        assert spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)

    def test_spearman_on_constant_series(self):
        """Корреляция с постоянной величиной не определена и даёт ноль."""
        assert spearman([5, 5, 5], [1, 2, 3]) == 0.0

    def test_spearman_rejects_different_lengths(self):
        """Сравнение ранжирований разной длины не имеет смысла."""
        with pytest.raises(MCDAError):
            spearman([1, 2, 3], [1, 2])

    def test_top_agreement_ignores_tail(self):
        """Совпадение головы списка не зависит от согласия в хвосте."""
        first = [100, 90, 10, 9, 8]
        second = [100, 90, 8, 9, 10]

        assert rank_agreement(first, second, top=2) == 1.0
        assert spearman(first, second) < 1.0


# ---------------------------------------------------------------------------
# Устойчивость
# ---------------------------------------------------------------------------
class TestSensitivity:
    """Возмущение весов и его последствия."""

    def test_perturbation_preserves_sum(self):
        """Сумма весов после возмущения остаётся равной единице."""
        result = perturb_weights([0.4, 0.3, 0.3], 0, 0.2)

        assert sum(result) == pytest.approx(1.0)
        assert result[0] > 0.4

    def test_perturbation_keeps_proportions_of_others(self):
        """Прочие веса уменьшаются пропорционально, а не поровну."""
        result = perturb_weights([0.5, 0.3, 0.2], 0, 0.2)

        assert result[1] / result[2] == pytest.approx(0.3 / 0.2)

    def test_rejects_unknown_criterion(self):
        """Возмущение несуществующего критерия — ошибка вызова."""
        with pytest.raises(MCDAError):
            perturb_weights([0.5, 0.5], 5, 0.1)

    def test_dominant_criterion_ranking_is_robust(self):
        """Ранжирование, определяемое одним критерием, устойчиво к весам."""
        matrix = [[90, 85], [60, 55], [30, 25]]

        report = sensitivity(matrix, [0.5, 0.5])

        assert report.is_robust
        assert report.worst_correlation == pytest.approx(1.0)

    def test_conflicting_criteria_ranking_is_fragile(self):
        """Противоположные критерии делают ранжирование зависимым от весов."""
        matrix = [[100, 0], [51, 49], [49, 51], [0, 100]]

        report = sensitivity(matrix, [0.5, 0.5], deltas=(0.3,))

        assert report.worst_correlation < 1.0

    def test_single_alternative_yields_empty_report(self):
        """Устойчивость ранжирования из одного кандидата не определена."""
        report = sensitivity([[50, 50]], [0.5, 0.5])

        assert report.outcomes == []
