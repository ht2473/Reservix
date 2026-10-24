"""Проверка правил предметной области, заданных в моделях."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from django.core.exceptions import ValidationError
from django.db.utils import IntegrityError
from django.utils import timezone

from apps.analytics.models import NineBoxPlacement
from apps.assessment.models import AssessmentScore
from apps.competencies.models import CompetencyRequirement
from apps.people.models import EducationLevel, Employee, EmployeeStatus, Mobility
from apps.reserve.models import ReserveEntry, ReserveStatus
from core.constants import NineBoxCell, ReadinessLevel
from tests.factories import PositionProfileFactory
from tests.factories.domain import (
    CompetencyFactory,
    CompetencyModelFactory,
    CompetencyRequirementFactory,
    DevelopmentActivityFactory,
    DevelopmentPlanFactory,
    EmployeeFactory,
    KeyPositionFactory,
    ReserveEntryFactory,
    SuccessionPlanFactory,
    SuccessionSlateFactory,
)

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Сотрудник
# ---------------------------------------------------------------------------
class TestEmployee:
    """Производные характеристики карточки сотрудника."""

    def test_tenure_counts_from_hire_date(self):
        """Стаж считается от даты приёма и выражается в месяцах."""
        employee = EmployeeFactory(hire_date=timezone.localdate() - dt.timedelta(days=365 * 3))
        assert 35 <= employee.tenure_months <= 37

    def test_dismissed_employee_tenure_is_frozen(self):
        """У уволенного стаж фиксируется датой увольнения."""
        employee = EmployeeFactory(
            hire_date=timezone.localdate() - dt.timedelta(days=365 * 4),
            status=EmployeeStatus.DISMISSED,
            dismissal_date=timezone.localdate() - dt.timedelta(days=365),
        )
        assert 34 <= employee.tenure_months <= 38

    def test_external_candidate_has_no_company_tenure(self):
        """Внешний кандидат не имеет стажа в организации."""
        employee = EmployeeFactory(
            is_external=True,
            branch=None,
            department=None,
            position=None,
            position_title="Senior Developer (Yandex)",
            hire_date=timezone.localdate() - dt.timedelta(days=365 * 3),
        )
        assert employee.tenure_months == 0
        assert employee.position_tenure_months == 0
        assert employee.total_experience_months == employee.prior_experience_months

    def test_total_experience_includes_prior(self):
        """Совокупный опыт складывается из стажа до приёма и в организации."""
        employee = EmployeeFactory(
            hire_date=timezone.localdate() - dt.timedelta(days=365 * 2),
            prior_experience_months=48,
        )
        assert employee.total_experience_months >= 71

    def test_profile_match_reports_each_requirement(self, position):
        """Сопоставление с профилем возвращает разбор по требованиям."""
        profile = PositionProfileFactory(
            position=position,
            min_education=EducationLevel.MASTER,
            min_total_experience_years=10,
            min_relevant_experience_years=1,
            min_company_tenure_months=12,
            requires_management_experience=True,
            min_subordinates=3,
        )
        employee = EmployeeFactory(
            education_level=EducationLevel.BACHELOR,
            hire_date=timezone.localdate() - dt.timedelta(days=365 * 3),
            prior_experience_months=0,
            management_experience_months=0,
            max_subordinates=0,
        )

        result = employee.matches_profile(profile)

        assert result["education"] is False
        assert result["total_experience"] is False
        assert result["management"] is False
        assert result["team_size"] is False
        assert result["company_tenure"] is True
        assert 0 < employee.profile_match_ratio(profile) < 1

    def test_dismissal_closes_employment_history(self):
        """Увольнение закрывает текущую запись кадровой истории."""
        employee = EmployeeFactory()
        record = employee.employment_history.create(
            position_title="Инженер",
            start_date=employee.hire_date,
        )

        employee.dismiss(reason="Переход в другую организацию")
        record.refresh_from_db()

        assert employee.status == EmployeeStatus.DISMISSED
        assert record.end_date == timezone.localdate()

    def test_search_finds_by_partial_last_name(self):
        """Поиск по подстроке находит сотрудника по фрагменту фамилии."""
        EmployeeFactory(last_name="Кузнецов", first_name="Андрей")
        assert Employee.objects.search("узнец").count() == 1

    def test_reserve_eligibility_excludes_dismissed(self):
        """Уволенный сотрудник не рассматривается в резерв."""
        EmployeeFactory(status=EmployeeStatus.ACTIVE)
        EmployeeFactory(
            status=EmployeeStatus.DISMISSED,
            dismissal_date=timezone.localdate(),
        )
        assert Employee.objects.reserve_eligible().count() == 1

    def test_mobility_default_is_city(self):
        """Умолчание мобильности — перемещение в пределах города."""
        assert EmployeeFactory().mobility == Mobility.CITY


# ---------------------------------------------------------------------------
# Кадровый резерв
# ---------------------------------------------------------------------------
class TestReserveWorkflow:
    """Воркфлоу статусов записи резерва."""

    def test_allowed_transition_writes_history(self, hr_user):
        """Смена статуса сопровождается записью в историю с указанием автора."""
        entry = ReserveEntryFactory(status=ReserveStatus.CANDIDATE)

        record = entry.transition_to(
            ReserveStatus.ACTIVE, user=hr_user, reason="Оценка подтвердила готовность"
        )

        entry.refresh_from_db()
        assert entry.status == ReserveStatus.ACTIVE
        assert record.old_status == ReserveStatus.CANDIDATE
        assert record.changed_by == hr_user
        assert entry.status_history.count() == 1

    def test_forbidden_transition_is_rejected(self):
        """Исключённого резервиста нельзя вернуть в резерв напрямую."""
        entry = ReserveEntryFactory(status=ReserveStatus.ACTIVE)
        entry.transition_to(ReserveStatus.EXCLUDED, reason="Перевод в другой филиал")

        with pytest.raises(ValueError, match="недопустим"):
            entry.transition_to(ReserveStatus.ACTIVE)

    def test_closing_transition_sets_exclusion_date(self):
        """Завершающий переход проставляет дату выбытия из резерва."""
        entry = ReserveEntryFactory(status=ReserveStatus.ACTIVE)
        entry.transition_to(ReserveStatus.APPOINTED)
        entry.refresh_from_db()
        assert entry.exclusion_date == timezone.localdate()

    @pytest.mark.parametrize(
        ("index_value", "expected"),
        [
            (92.0, ReadinessLevel.HIGH),
            (75.0, ReadinessLevel.HIGH),
            (60.0, ReadinessLevel.MEDIUM),
            (55.0, ReadinessLevel.MEDIUM),
            (30.0, ReadinessLevel.LOW),
        ],
    )
    def test_readiness_level_follows_index(self, index_value, expected):
        """Уровень готовности выводится из индекса, а не назначается вручную."""
        entry = ReserveEntryFactory()
        entry.apply_readiness(index_value)
        entry.refresh_from_db()

        assert entry.readiness_level == expected
        assert entry.readiness_index == Decimal(str(round(index_value, 2)))
        assert entry.readiness_calculated_at is not None

    def test_same_employee_may_target_two_positions(self, grade, department):
        """Резервист может состоять в резерве на несколько должностей."""
        from tests.factories import PositionFactory

        employee = EmployeeFactory()
        first = PositionFactory(name="Руководитель отдела", grade=grade, department=department)
        second = PositionFactory(name="Руководитель проектов", grade=grade, department=department)

        ReserveEntryFactory(employee=employee, target_position=first)
        ReserveEntryFactory(employee=employee, target_position=second)

        assert ReserveEntry.objects.for_employee(employee.pk).count() == 2

    def test_duplicate_active_pair_is_rejected(self, position):
        """Две действующие записи на одну должность недопустимы."""
        employee = EmployeeFactory()
        ReserveEntryFactory(employee=employee, target_position=position)

        with pytest.raises(IntegrityError):
            ReserveEntryFactory(employee=employee, target_position=position)

    def test_months_in_reserve_counts_from_inclusion(self):
        """Срок пребывания в резерве считается от даты включения."""
        entry = ReserveEntryFactory(inclusion_date=timezone.localdate() - dt.timedelta(days=365))
        assert 11 <= entry.months_in_reserve <= 13


# ---------------------------------------------------------------------------
# Компетенции
# ---------------------------------------------------------------------------
class TestCompetencies:
    """Требования модели компетенций и расчёт разрыва."""

    def test_missing_assessment_counts_as_full_gap(self):
        """Отсутствие оценки трактуется как полный разрыв."""
        requirement = CompetencyRequirementFactory(target_level=4)
        assert requirement.gap_for(None) == 4.0
        assert requirement.is_satisfied_by(None) is False

    def test_exceeding_target_is_not_a_gap(self):
        """Превышение целевого уровня разрывом не считается."""
        requirement = CompetencyRequirementFactory(target_level=3)
        assert requirement.gap_for(5) == 0.0
        assert requirement.is_satisfied_by(5) is True

    def test_minimum_level_defines_admission(self):
        """Минимально допустимый уровень задаёт порог рассмотрения кандидата."""
        requirement = CompetencyRequirementFactory(target_level=5, minimum_level=3)
        assert requirement.is_satisfied_by(3) is True
        assert requirement.is_satisfied_by(2) is False

    def test_minimum_above_target_is_rejected(self):
        """Минимальный уровень выше целевого противоречив и не сохраняется."""
        with pytest.raises(IntegrityError):
            CompetencyRequirementFactory(target_level=3, minimum_level=4)

    def test_weight_balance_is_checked(self):
        """Сумма весов требований модели должна образовывать распределение."""
        model = CompetencyModelFactory()
        CompetencyRequirementFactory(competency_model=model, weight=Decimal("0.400"))
        CompetencyRequirementFactory(competency_model=model, weight=Decimal("0.600"))

        assert model.is_weight_balanced is True

        CompetencyRequirementFactory(competency_model=model, weight=Decimal("0.300"))
        assert model.is_weight_balanced is False

    def test_normalized_weights_sum_to_one(self):
        """Веса приводятся к сумме единица при чтении, а не при сохранении."""
        model = CompetencyModelFactory()
        CompetencyRequirementFactory(competency_model=model, weight=Decimal("3.000"))
        CompetencyRequirementFactory(competency_model=model, weight=Decimal("1.000"))

        weights = model.normalized_weights()
        assert sum(weights.values()) == pytest.approx(Decimal("1"), abs=Decimal("0.001"))

    def test_full_scale_detection(self):
        """Компетенция с пробелами в шкале распознаётся как неполная."""
        complete = CompetencyFactory()
        incomplete = CompetencyFactory(scale=False)

        assert complete.has_full_scale is True
        assert incomplete.has_full_scale is False

    def test_duplicate_requirement_in_model_is_rejected(self):
        """Одна компетенция входит в модель не более одного раза."""
        model = CompetencyModelFactory()
        competency = CompetencyFactory()
        CompetencyRequirement.objects.create(
            competency_model=model, competency=competency, target_level=3
        )

        with pytest.raises(IntegrityError):
            CompetencyRequirement.objects.create(
                competency_model=model, competency=competency, target_level=4
            )


# ---------------------------------------------------------------------------
# Оценка
# ---------------------------------------------------------------------------
class TestAssessmentScore:
    """Перевод между шкалой компетенций и стобалльной шкалой."""

    @pytest.mark.parametrize(
        ("level", "expected"),
        [(1, Decimal("0.00")), (3, Decimal("50.00")), (5, Decimal("100.00"))],
    )
    def test_level_to_score(self, level, expected):
        """Уровень шкалы переводится в балл линейно."""
        assert AssessmentScore.level_to_score(level) == expected

    @pytest.mark.parametrize(
        ("score", "expected"),
        [(0, Decimal("1.0")), (50, Decimal("3.0")), (100, Decimal("5.0"))],
    )
    def test_score_to_level(self, score, expected):
        """Балл переводится обратно в уровень шкалы компетенций."""
        assert AssessmentScore.score_to_level(score) == expected

    def test_raw_score_is_filled_on_save(self):
        """При сохранении балл 0–100 достраивается по уровню."""
        from tests.factories.domain import AssessmentScoreFactory

        score = AssessmentScoreFactory(level=Decimal("4.0"))
        assert score.raw_score == Decimal("75.00")


# ---------------------------------------------------------------------------
# Матрица 9-box
# ---------------------------------------------------------------------------
class TestNineBox:
    """Определение ячейки матрицы по координатам."""

    @pytest.mark.parametrize(
        ("performance", "potential", "expected"),
        [
            (90, 90, NineBoxCell.STAR),
            (90, 20, NineBoxCell.TRUSTED_PROFESSIONAL),
            (20, 90, NineBoxCell.ENIGMA),
            (20, 20, NineBoxCell.UNDERPERFORMER),
            (60, 60, NineBoxCell.CORE_PLAYER),
            (60, 90, NineBoxCell.HIGH_POTENTIAL),
        ],
    )
    def test_cell_resolution(self, performance, potential, expected):
        """Ячейка выводится из координат по границам зон из настроек."""
        assert NineBoxPlacement.resolve_cell(performance, potential) == expected

    def test_cell_is_computed_on_save(self):
        """Ячейка вычисляется при сохранении и не задаётся вручную."""
        from tests.factories.domain import NineBoxPlacementFactory

        placement = NineBoxPlacementFactory(
            performance=Decimal("95.00"), potential=Decimal("95.00")
        )
        assert placement.cell == NineBoxCell.STAR
        assert placement.is_talent is True


# ---------------------------------------------------------------------------
# Преемственность
# ---------------------------------------------------------------------------
class TestSuccession:
    """Приоритет планирования и обеспеченность критичных должностей."""

    def test_priority_is_product_of_criticality_and_risk(self):
        """Приоритет — произведение критичности на риск освобождения."""
        urgent = KeyPositionFactory(criticality=3, vacancy_risk=3)
        important = KeyPositionFactory(criticality=5, vacancy_risk=1)

        assert urgent.priority_score > important.priority_score

    def test_coverage_counts_successors_in_active_plans(self):
        """Обеспеченность считается по преемникам в действующих планах."""
        key_position = KeyPositionFactory(required_successors=2)
        plan = SuccessionPlanFactory(key_position=key_position)
        SuccessionSlateFactory(plan=plan, rank=1)

        assert key_position.successor_count == 1
        assert key_position.coverage == 0.5
        assert key_position.is_covered is False

        SuccessionSlateFactory(plan=plan, rank=2)
        assert key_position.is_covered is True

    def test_single_active_plan_per_position(self):
        """Действующий план по должности может быть только один."""
        key_position = KeyPositionFactory()
        SuccessionPlanFactory(key_position=key_position)

        with pytest.raises(IntegrityError):
            SuccessionPlanFactory(key_position=key_position)


# ---------------------------------------------------------------------------
# Развитие
# ---------------------------------------------------------------------------
class TestDevelopment:
    """Прогресс индивидуального плана развития."""

    def test_completion_uses_activity_weights(self):
        """Процент выполнения учитывает вес мероприятий."""
        plan = DevelopmentPlanFactory()
        heavy = DevelopmentActivityFactory(plan=plan, weight=3)
        DevelopmentActivityFactory(plan=plan, weight=1)

        assert plan.recalculate_completion() == 0

        heavy.complete()
        plan.refresh_from_db()
        assert plan.completion_rate == 75

    def test_completed_activity_requires_date(self):
        """Выполненное мероприятие без даты выполнения не сохраняется."""
        with pytest.raises(IntegrityError):
            DevelopmentActivityFactory(status="completed", completion_date=None)

    def test_overdue_is_computed_from_date(self):
        """Просрочка определяется датой, а не хранимым статусом."""
        activity = DevelopmentActivityFactory(due_date=timezone.localdate() - dt.timedelta(days=1))
        assert activity.is_overdue is True
        assert activity.plan.overdue_count == 1

    def test_activity_period_validity_is_enforced(self):
        """Срок выполнения не может предшествовать дате начала."""
        with pytest.raises(IntegrityError):
            DevelopmentActivityFactory(
                start_date=timezone.localdate(),
                due_date=timezone.localdate() - dt.timedelta(days=5),
            )


# ---------------------------------------------------------------------------
# Проверки на уровне формы модели
# ---------------------------------------------------------------------------
class TestModelValidation:
    """Проверки, выполняемые при полной валидации объекта."""

    def test_employee_email_is_normalized(self):
        """Адрес почты приводится к нижнему регистру без пробелов."""
        employee = EmployeeFactory.build(email="  Employee@Meridian-Soft.RU ")
        employee.clean()
        assert employee.email == "employee@meridian-soft.ru"

    def test_scale_level_outside_range_is_rejected(self):
        """Уровень вне шкалы компетенций отклоняется проверкой."""
        from apps.competencies.models import CompetencyScaleLevel

        level = CompetencyScaleLevel(
            competency=CompetencyFactory(scale=False),
            level=9,
            name="Недопустимый уровень",
            indicators="—",
        )
        with pytest.raises(ValidationError):
            level.full_clean()
