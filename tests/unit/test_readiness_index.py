"""Проверка правил композитного индекса готовности."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from django.utils import timezone

from apps.analytics import criteria as criteria_module
from apps.analytics.criteria import CriterionValue
from apps.analytics.models import (
    AggregationMethod,
    CalibrationStatus,
    NineBoxPlacement,
    ReadinessSnapshot,
    ScoringScheme,
    WeightingMethod,
)
from apps.analytics.services import (
    AnalyticsError,
    apply_expert_weights,
    build_ahp_weights,
    build_entropy_weights,
    calibrate,
    calibration_effect,
    compare_schemes,
    complete_calibration,
    compute_index,
    ensure_criteria,
    ensure_default_scheme,
    nine_box_grid,
    place_employee,
    readiness_forecast,
    recalculate,
    resolve_scheme,
)
from core.constants import NineBoxCell, ReadinessLevel
from tests.factories.domain import (
    CalibrationSessionFactory,
    EmployeeFactory,
    NineBoxPlacementFactory,
    ReserveEntryFactory,
    ScoringCriterionFactory,
    ScoringSchemeFactory,
    ScoringWeightFactory,
)

pytestmark = [pytest.mark.unit, pytest.mark.django_db]


@pytest.fixture
def scheme(db):
    """Схема из двух критериев с равными весами — минимальная для расчёта."""
    scoring = ScoringSchemeFactory(code="test-scheme", name="Проверочная схема")
    first = ScoringCriterionFactory(code="test-first", source="competency_match", sort_order=10)
    second = ScoringCriterionFactory(code="test-second", source="experience", sort_order=20)
    ScoringWeightFactory(scheme=scoring, criterion=first, weight=Decimal("0.60000"))
    ScoringWeightFactory(scheme=scoring, criterion=second, weight=Decimal("0.40000"))
    return scoring


def stub_values(monkeypatch, values: dict[str, CriterionValue]) -> None:
    """Подменяет расчёт частных критериев заданными значениями."""

    def evaluate(source, entry, context):
        return values[source]

    monkeypatch.setattr(criteria_module, "evaluate", evaluate)


def measured(source: str, value: float) -> CriterionValue:
    """Значение критерия, обеспеченное данными."""
    return CriterionValue(source=source, raw=value, normalized=value, has_data=True, unit="баллы")


# ---------------------------------------------------------------------------
# Свёртка и обеспеченность данными
# ---------------------------------------------------------------------------
class TestIndexComposition:
    """Правила сложения частных критериев в композитный индекс."""

    def test_weighted_sum_of_full_data(self, scheme, position, monkeypatch):
        """Индекс равен взвешенной сумме частных оценок."""
        entry = ReserveEntryFactory(target_position=position)
        stub_values(
            monkeypatch,
            {
                "competency_match": measured("competency_match", 80),
                "experience": measured("experience", 50),
            },
        )

        result = compute_index([entry], scheme)[0]

        assert result.index_value == pytest.approx(68.0)
        assert result.covered_weight == pytest.approx(1.0)
        assert result.is_reliable

    def test_missing_criterion_redistributes_weight(self, scheme, position, monkeypatch):
        """Вес критерия без данных переходит к остальным пропорционально."""
        entry = ReserveEntryFactory(target_position=position)
        stub_values(
            monkeypatch,
            {
                "competency_match": measured("competency_match", 80),
                "experience": CriterionValue.missing("experience", "нет кадровой истории"),
            },
        )

        result = compute_index([entry], scheme)[0]

        assert result.index_value == pytest.approx(80.0)
        assert result.covered_weight == pytest.approx(0.6)
        assert [item.code for item in result.missing] == ["test-second"]

    def test_coverage_below_two_thirds_is_unreliable(self, scheme, position, monkeypatch):
        """Обеспеченность ниже двух третей помечает расчёт недостоверным."""
        entry = ReserveEntryFactory(target_position=position)
        stub_values(
            monkeypatch,
            {
                "competency_match": CriterionValue.missing("competency_match", "нет модели"),
                "experience": measured("experience", 90),
            },
        )

        result = compute_index([entry], scheme)[0]

        assert result.covered_weight == pytest.approx(0.4)
        assert not result.is_reliable

    def test_no_data_gives_zero_without_failing(self, scheme, position, monkeypatch):
        """Полное отсутствие данных даёт ноль, а не отказ расчёта."""
        entry = ReserveEntryFactory(target_position=position)
        stub_values(
            monkeypatch,
            {
                "competency_match": CriterionValue.missing("competency_match", "нет модели"),
                "experience": CriterionValue.missing("experience", "нет истории"),
            },
        )

        result = compute_index([entry], scheme)[0]

        assert result.index_value == 0.0
        assert result.covered_weight == 0.0

    def test_weakest_ordered_by_lost_points(self, scheme, position, monkeypatch):
        """Первым к развитию идёт критерий, который дороже обходится."""
        entry = ReserveEntryFactory(target_position=position)
        stub_values(
            monkeypatch,
            {
                "competency_match": measured("competency_match", 40),
                "experience": measured("experience", 30),
            },
        )

        result = compute_index([entry], scheme)[0]

        assert next(item.code for item in result.weakest) == "test-first"

    def test_level_follows_thresholds(self, scheme, position, monkeypatch):
        """Уровень готовности выводится из индекса, а не задаётся руками."""
        entry = ReserveEntryFactory(target_position=position)
        stub_values(
            monkeypatch,
            {
                "competency_match": measured("competency_match", 90),
                "experience": measured("experience", 90),
            },
        )

        assert compute_index([entry], scheme)[0].level == ReadinessLevel.HIGH

    def test_rank_carries_denominator(self, scheme, position, monkeypatch):
        """Ранг сохраняется вместе с числом претендентов."""
        stronger = ReserveEntryFactory(target_position=position)
        weaker = ReserveEntryFactory(target_position=position)
        values = {
            stronger.pk: 90.0,
            weaker.pk: 40.0,
        }

        def evaluate(source, entry, context):
            return measured(source, values[entry.pk])

        monkeypatch.setattr(criteria_module, "evaluate", evaluate)

        results = {item.entry_id: item for item in compute_index([stronger, weaker], scheme)}

        assert results[stronger.pk].rank == 1
        assert results[stronger.pk].candidates_total == 2
        assert results[weaker.pk].rank == 2


class TestTopsisAggregation:
    """Свёртка методом близости к идеальному решению."""

    def test_topsis_requires_complete_criteria(self, scheme, position, monkeypatch):
        """Запись с пропуском сохраняет значение взвешенной суммы."""
        scheme.aggregation = AggregationMethod.TOPSIS
        scheme.save(update_fields=["aggregation"])

        complete = ReserveEntryFactory(target_position=position)
        partial = ReserveEntryFactory(target_position=position)

        def evaluate(source, entry, context):
            if entry.pk == partial.pk and source == "experience":
                return CriterionValue.missing(source, "нет данных")
            return measured(source, 70.0)

        monkeypatch.setattr(criteria_module, "evaluate", evaluate)

        results = {item.entry_id: item for item in compute_index([complete, partial], scheme)}

        assert results[partial.pk].aggregation == AggregationMethod.WEIGHTED_SUM


# ---------------------------------------------------------------------------
# Сохранение расчёта
# ---------------------------------------------------------------------------
class TestRecalculation:
    """Запись результатов расчёта."""

    def test_default_scheme_updates_entries(self, position, monkeypatch, hr_user):
        """Схема по умолчанию обновляет рабочий индекс записи резерва."""
        ensure_criteria()
        default = ensure_default_scheme(author=hr_user)
        entry = ReserveEntryFactory(target_position=position)
        monkeypatch.setattr(
            criteria_module, "evaluate", lambda source, e, c: measured(source, 88.0)
        )

        result = recalculate(scheme=default, notify_changes=False)

        entry.refresh_from_db()
        assert result.is_operational
        assert entry.readiness_index == pytest.approx(Decimal("88.00"))
        assert entry.readiness_level == ReadinessLevel.HIGH

    def test_alternative_scheme_leaves_entries_untouched(
        self, scheme, position, monkeypatch, hr_user
    ):
        """Прогон по не основной схеме не меняет уровней готовности."""
        ensure_criteria()
        ensure_default_scheme(author=hr_user)
        entry = ReserveEntryFactory(target_position=position, readiness_index=Decimal("30.00"))
        monkeypatch.setattr(
            criteria_module, "evaluate", lambda source, e, c: measured(source, 95.0)
        )

        result = recalculate(scheme=scheme, notify_changes=False)

        entry.refresh_from_db()
        assert not result.is_operational
        assert entry.readiness_index == pytest.approx(Decimal("30.00"))
        assert ReadinessSnapshot.objects.filter(entry=entry, scheme=scheme).count() == 1

    def test_previous_snapshot_stops_being_current(self, scheme, position, monkeypatch):
        """Текущим остаётся один расчёт по схеме, прежние сохраняются в истории."""
        entry = ReserveEntryFactory(target_position=position)
        monkeypatch.setattr(
            criteria_module, "evaluate", lambda source, e, c: measured(source, 60.0)
        )

        recalculate(scheme=scheme, notify_changes=False)
        recalculate(scheme=scheme, notify_changes=False)

        snapshots = ReadinessSnapshot.objects.filter(entry=entry, scheme=scheme)
        assert snapshots.count() == 2
        assert snapshots.filter(is_current=True).count() == 1

    def test_snapshot_keeps_applied_weight(self, scheme, position, monkeypatch):
        """В снимке сохраняется применённый вес, а не только оценка."""
        entry = ReserveEntryFactory(target_position=position)
        monkeypatch.setattr(
            criteria_module, "evaluate", lambda source, e, c: measured(source, 70.0)
        )

        recalculate(scheme=scheme, notify_changes=False)

        payload = ReadinessSnapshot.objects.get(entry=entry, scheme=scheme).criterion_values
        assert payload["test-first"]["weight"] == pytest.approx(0.6)
        assert payload["_meta"]["coverage"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Схемы весов
# ---------------------------------------------------------------------------
class TestSchemes:
    """Три способа получения весов и их пригодность к расчёту."""

    def test_expert_weights_are_normalised(self, scheme):
        """Экспертные баллы важности приводятся к сумме единицы."""
        criteria = [weight.criterion for weight in scheme.weights.all()]
        values = {criteria[0].pk: Decimal("7"), criteria[1].pk: Decimal("3")}

        apply_expert_weights(scheme, values)

        weights = {w.criterion_id: w for w in scheme.weights.all()}
        assert weights[criteria[0].pk].weight == pytest.approx(Decimal("0.70000"))
        assert weights[criteria[0].pk].raw_value == Decimal("7")

    def test_expert_weights_reject_zero_sum(self, scheme):
        """Нулевая сумма важности не образует распределения."""
        criteria = [weight.criterion for weight in scheme.weights.all()]

        with pytest.raises(AnalyticsError):
            apply_expert_weights(scheme, {criterion.pk: Decimal("0") for criterion in criteria})

    def test_ahp_stores_matrix_and_ratio(self, scheme):
        """Матрица суждений сохраняется вместе с полученными весами."""
        criteria = [weight.criterion for weight in scheme.weights.all()]

        outcome = build_ahp_weights(scheme, {(0, 1): 3}, criteria)

        scheme.refresh_from_db()
        assert scheme.method == WeightingMethod.AHP
        assert scheme.pairwise_matrix["matrix"][0][1] == pytest.approx(3.0)
        assert outcome.consistency_ratio == pytest.approx(0.0, abs=1e-6)
        assert scheme.is_consistent

    def test_inconsistent_scheme_is_not_usable(self, scheme):
        """Противоречивая схема сохраняется, но к расчёту не допускается."""
        criteria = list(
            ScoringSchemeFactory(code="ahp-three").weights.all()
        )  # пустой список — схема ниже собирается вручную
        assert criteria == []

        scoring = ScoringSchemeFactory(code="ahp-check", method=WeightingMethod.AHP)
        first = ScoringCriterionFactory(code="c1", source="competency_match")
        second = ScoringCriterionFactory(code="c2", source="performance")
        third = ScoringCriterionFactory(code="c3", source="experience")

        build_ahp_weights(scoring, {(0, 1): 9, (1, 2): 9, (0, 2): 1 / 9}, [first, second, third])

        scoring.refresh_from_db()
        assert not scoring.is_consistent
        assert not scoring.is_ready
        with pytest.raises(AnalyticsError):
            resolve_scheme(scoring)

    def test_entropy_requires_sample(self, scheme, position):
        """Веса, выведенные из горстки записей, отражают случайность выборки."""
        ReserveEntryFactory(target_position=position)
        criteria = [weight.criterion for weight in scheme.weights.all()]

        with pytest.raises(AnalyticsError):
            build_entropy_weights(scheme, criteria)

    def test_scheme_with_incomplete_weights_is_rejected(self, position):
        """Схема, веса которой не образуют распределения, к расчёту не годится."""
        scoring = ScoringSchemeFactory(code="partial")
        criterion = ScoringCriterionFactory(code="only", source="competency_match")
        ScoringWeightFactory(scheme=scoring, criterion=criterion, weight=Decimal("0.30000"))

        with pytest.raises(AnalyticsError):
            resolve_scheme(scoring)


class TestSchemeComparison:
    """Сравнение ранжирований разных схем."""

    def test_requires_two_schemes(self, scheme):
        """Одна схема согласуется сама с собой тождественно."""
        with pytest.raises(AnalyticsError):
            compare_schemes([scheme])

    def test_opposite_weights_reverse_ranking(self, position, monkeypatch):
        """Противоположные веса дают противоположный порядок."""
        first_criterion = ScoringCriterionFactory(code="cmp-a", source="competency_match")
        second_criterion = ScoringCriterionFactory(code="cmp-b", source="experience")

        left = ScoringSchemeFactory(code="cmp-left")
        ScoringWeightFactory(scheme=left, criterion=first_criterion, weight=Decimal("0.90000"))
        ScoringWeightFactory(scheme=left, criterion=second_criterion, weight=Decimal("0.10000"))

        right = ScoringSchemeFactory(code="cmp-right")
        ScoringWeightFactory(scheme=right, criterion=first_criterion, weight=Decimal("0.10000"))
        ScoringWeightFactory(scheme=right, criterion=second_criterion, weight=Decimal("0.90000"))

        strong_competency = ReserveEntryFactory(target_position=position)
        strong_experience = ReserveEntryFactory(target_position=position)
        table = {
            (strong_competency.pk, "competency_match"): 95.0,
            (strong_competency.pk, "experience"): 20.0,
            (strong_experience.pk, "competency_match"): 20.0,
            (strong_experience.pk, "experience"): 95.0,
        }

        monkeypatch.setattr(
            criteria_module,
            "evaluate",
            lambda source, entry, context: measured(source, table[(entry.pk, source)]),
        )

        comparison = compare_schemes([left, right])

        assert comparison.correlations[("cmp-left", "cmp-right")] == pytest.approx(-1.0)
        assert comparison.min_correlation < 0.7


# ---------------------------------------------------------------------------
# Матрица 9-box
# ---------------------------------------------------------------------------
class TestNineBox:
    """Размещение по осям результативности и потенциала."""

    @pytest.mark.parametrize(
        ("performance", "potential", "cell"),
        [
            (90, 90, NineBoxCell.STAR),
            (90, 20, NineBoxCell.TRUSTED_PROFESSIONAL),
            (20, 90, NineBoxCell.ENIGMA),
            (20, 20, NineBoxCell.UNDERPERFORMER),
            (55, 55, NineBoxCell.CORE_PLAYER),
        ],
    )
    def test_cell_follows_coordinates(self, performance, potential, cell):
        """Ячейка вычисляется по координатам, а не выбирается вручную."""
        assert NineBoxPlacement.resolve_cell(performance, potential) == cell

    def test_new_placement_retires_previous(self, branch, department):
        """Прежняя оценка сохраняется со снятым признаком действующей."""
        employee = EmployeeFactory(branch=branch, department=department)
        NineBoxPlacementFactory(
            employee=employee, performance=Decimal("40"), potential=Decimal("40")
        )

        place_employee(employee, performance=85, potential=85)

        placements = NineBoxPlacement.objects.filter(employee=employee)
        assert placements.count() == 2
        assert placements.filter(is_current=True).count() == 1

    def test_talent_cell_marks_key_employee(self, branch, department):
        """Отметка ключевого сотрудника выставляется системой по ячейке."""
        employee = EmployeeFactory(branch=branch, department=department, is_key_talent=False)

        place_employee(employee, performance=90, potential=90)

        employee.refresh_from_db()
        assert employee.is_key_talent

    def test_calibration_requires_reason(self, branch, department, manager_user):
        """Перемещение без обоснования превращает калибровку в торг."""
        placement = NineBoxPlacementFactory(
            employee=EmployeeFactory(branch=branch, department=department)
        )

        with pytest.raises(AnalyticsError):
            calibrate(placement, performance=50, potential=50, comment="  ", actor=manager_user)

    def test_calibration_keeps_first_initial_values(self, branch, department, manager_user):
        """«До калибровки» означает состояние до начала процедуры."""
        placement = NineBoxPlacementFactory(
            employee=EmployeeFactory(branch=branch, department=department),
            performance=Decimal("80"),
            potential=Decimal("80"),
        )

        calibrate(
            placement, performance=60, potential=60, comment="Первое обсуждение", actor=manager_user
        )
        calibrate(
            placement, performance=50, potential=50, comment="Второе обсуждение", actor=manager_user
        )

        placement.refresh_from_db()
        assert placement.initial_performance == pytest.approx(Decimal("80.00"))
        assert placement.performance == pytest.approx(Decimal("50.00"))

    def test_grid_reports_concentration(self, branch, department):
        """Перекос распределения — самостоятельный результат."""
        for _ in range(9):
            NineBoxPlacementFactory(
                employee=EmployeeFactory(branch=branch, department=department),
                performance=Decimal("55"),
                potential=Decimal("55"),
            )
        NineBoxPlacementFactory(
            employee=EmployeeFactory(branch=branch, department=department),
            performance=Decimal("95"),
            potential=Decimal("95"),
        )

        grid = nine_box_grid(with_people=False)

        assert grid.total == 10
        assert grid.concentration == pytest.approx(90.0)
        assert not grid.is_discriminating

    def test_calibration_effect_measures_shift(self, branch, department, manager_user):
        """Сдвиг оценок характеризует оценочную культуру, а не сотрудников."""
        session = CalibrationSessionFactory()
        placement = NineBoxPlacementFactory(
            employee=EmployeeFactory(branch=branch, department=department),
            performance=Decimal("80"),
            potential=Decimal("80"),
        )

        calibrate(
            placement,
            performance=70,
            potential=75,
            comment="Результат достигнут за счёт проекта предшественника",
            actor=manager_user,
            session=session,
        )

        effect = calibration_effect(session)
        assert effect["adjusted"] == 1
        assert effect["performance_shift"] == pytest.approx(-10.0)

    def test_completed_session_cannot_be_closed_twice(self, hr_user):
        """Повторное закрытие сессии переписало бы её протокол."""
        session = CalibrationSessionFactory(status=CalibrationStatus.IN_PROGRESS)
        complete_calibration(session, decisions="Рассмотрено 10 оценок, изменено 2")

        with pytest.raises(AnalyticsError):
            complete_calibration(session, decisions="Повтор")


# ---------------------------------------------------------------------------
# Прогноз
# ---------------------------------------------------------------------------
class TestForecast:
    """Оценка срока достижения готовности."""

    def test_requires_three_observations(self, scheme, position):
        """Две точки задают прямую всегда, и прогноз по ним — тавтология."""
        entry = ReserveEntryFactory(target_position=position)
        moment = timezone.now()
        for offset, value in ((60, "40.00"), (30, "45.00")):
            ReadinessSnapshot.objects.create(
                entry=entry,
                scheme=scheme,
                index_value=Decimal(value),
                readiness_level=ReadinessLevel.LOW,
                calculated_at=moment - dt.timedelta(days=offset),
                is_current=False,
            )

        forecast = readiness_forecast(entry, scheme=scheme)

        assert not forecast.is_available
        assert forecast.observations == 2

    def test_growing_index_yields_date(self, scheme, position):
        """Растущий индекс даёт срок, выраженный датой и числом месяцев."""
        entry = ReserveEntryFactory(target_position=position)
        moment = timezone.now()
        for offset, value in ((180, "40.00"), (120, "50.00"), (60, "60.00"), (0, "70.00")):
            ReadinessSnapshot.objects.create(
                entry=entry,
                scheme=scheme,
                index_value=Decimal(value),
                readiness_level=ReadinessLevel.MEDIUM,
                calculated_at=moment - dt.timedelta(days=offset),
                is_current=offset == 0,
            )

        forecast = readiness_forecast(entry, scheme=scheme)

        assert forecast.is_available
        assert forecast.slope_per_month > 0
        assert forecast.months_to_target == pytest.approx(1.0, abs=0.5)

    def test_flat_index_reports_absence_of_growth(self, scheme, position):
        """Отсутствие роста — вывод, а не пробел."""
        entry = ReserveEntryFactory(target_position=position)
        moment = timezone.now()
        for offset in (180, 120, 60, 0):
            ReadinessSnapshot.objects.create(
                entry=entry,
                scheme=scheme,
                index_value=Decimal("50.00"),
                readiness_level=ReadinessLevel.LOW,
                calculated_at=moment - dt.timedelta(days=offset),
                is_current=offset == 0,
            )

        forecast = readiness_forecast(entry, scheme=scheme)

        assert not forecast.is_available
        assert "не растёт" in forecast.note


# ---------------------------------------------------------------------------
# Справочник
# ---------------------------------------------------------------------------
class TestCriteriaRegistry:
    """Первичное заполнение справочника критериев."""

    def test_creates_six_criteria(self, db):
        """Методика состоит из шести частных критериев."""
        criteria = ensure_criteria()

        assert len(criteria) == 6
        assert {criterion.source for criterion in criteria} == set(criteria_module.known_sources())

    def test_is_idempotent(self, db):
        """Повторная сверка не создаёт дубликатов."""
        ensure_criteria()
        second = ensure_criteria()

        assert len(second) == 6

    def test_keeps_custom_name(self, db):
        """Наименование, заданное кадровой службой, сверкой не затирается."""
        created = ensure_criteria()[0]
        created.name = "Соответствие профилю роли"
        created.save(update_fields=["name"])

        ensure_criteria()

        created.refresh_from_db()
        assert created.name == "Соответствие профилю роли"

    def test_default_scheme_is_single(self, db, hr_user):
        """Схема по умолчанию одна: иначе результат зависел бы от порядка строк."""
        ensure_default_scheme(author=hr_user)
        ensure_default_scheme(author=hr_user)

        assert ScoringScheme.objects.filter(is_default=True).count() == 1
