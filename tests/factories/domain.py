"""Фабрики предметной области: люди, компетенции, оценка, резерв, развитие."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import factory
from factory.django import DjangoModelFactory

from django.utils import timezone

from apps.analytics.models import (
    CalibrationSession,
    CalibrationStatus,
    NineBoxPlacement,
    ReadinessSnapshot,
    ScoringCriterion,
    ScoringScheme,
    ScoringWeight,
    WeightingMethod,
)
from apps.assessment.models import (
    AssessmentCampaign,
    AssessmentMethod,
    AssessmentScore,
    AssessmentSession,
    CampaignStatus,
    SessionStatus,
)
from apps.competencies.models import (
    Competency,
    CompetencyCategory,
    CompetencyKind,
    CompetencyModel,
    CompetencyRequirement,
    CompetencyScaleLevel,
)
from apps.development.models import (
    ActivityStatus,
    ActivityType,
    DevelopmentActivity,
    DevelopmentPlan,
    LearningResource,
    Mentorship,
    MentorshipStatus,
    PlanStatus,
)
from apps.people.models import Employee, EmploymentChange, EmploymentHistory
from apps.reserve.models import (
    ReserveEntry,
    ReserveGroup,
    ReserveGroupKind,
    ReserveNomination,
    ReserveStatus,
)
from apps.succession.models import (
    Appointment,
    AppointmentKind,
    CriticalityLevel,
    KeyPosition,
    SuccessionPlan,
    SuccessionSlate,
    VacancyRisk,
)
from apps.succession.models import (
    PlanStatus as SuccessionPlanStatus,
)
from core.constants import ReadinessLevel
from tests.factories import BranchFactory, DepartmentFactory, PositionFactory, UserFactory

#: Дата, относительно которой строятся все умолчания.
TODAY = timezone.localdate


# ---------------------------------------------------------------------------
# Люди
# ---------------------------------------------------------------------------
class EmployeeFactory(DjangoModelFactory):
    """Карточка сотрудника со стажем около пяти лет."""

    class Meta:
        model = Employee

    personnel_number = factory.Sequence(lambda n: f"T-{n:05d}")
    last_name = factory.Iterator(["Абрамов", "Белов", "Гусев", "Дроздов", "Ершов"])
    first_name = factory.Iterator(["Павел", "Роман", "Тимур", "Никита", "Егор"])
    middle_name = factory.Iterator(["Павлович", "Романович", "Тимурович", "Никитич", "Егорович"])
    email = factory.Sequence(lambda n: f"employee{n}@meridian-soft.ru")
    phone = factory.Sequence(lambda n: f"+7910{n:07d}")
    birth_date = factory.LazyFunction(lambda: TODAY() - dt.timedelta(days=365 * 34))
    hire_date = factory.LazyFunction(lambda: TODAY() - dt.timedelta(days=365 * 5))
    position_since = factory.LazyFunction(lambda: TODAY() - dt.timedelta(days=365 * 2))
    branch = factory.SubFactory(BranchFactory)
    department = factory.SubFactory(DepartmentFactory)
    position = factory.SubFactory(PositionFactory)
    education = "МГТУ им. Баумана, программная инженерия, 2013"
    prior_experience_months = 36
    status = "active"


class EmploymentHistoryFactory(DjangoModelFactory):
    """Текущая запись кадровой истории сотрудника."""

    class Meta:
        model = EmploymentHistory

    employee = factory.SubFactory(EmployeeFactory)
    position_title = "Инженер-программист"
    change_type = EmploymentChange.HIRE
    start_date = factory.LazyAttribute(lambda obj: obj.employee.hire_date)


# ---------------------------------------------------------------------------
# Компетенции
# ---------------------------------------------------------------------------
class CompetencyCategoryFactory(DjangoModelFactory):
    """Категория справочника компетенций."""

    class Meta:
        model = CompetencyCategory
        django_get_or_create = ("code",)

    code = factory.Sequence(lambda n: f"category-{n}")
    name = factory.Sequence(lambda n: f"Категория компетенций {n}")
    kind = CompetencyKind.MANAGERIAL


class CompetencyFactory(DjangoModelFactory):
    """Компетенция справочника."""

    class Meta:
        model = Competency
        django_get_or_create = ("code",)
        skip_postgeneration_save = True

    code = factory.Sequence(lambda n: f"competency-{n}")
    name = factory.Sequence(lambda n: f"Компетенция {n}")
    category = factory.SubFactory(CompetencyCategoryFactory)
    kind = CompetencyKind.MANAGERIAL
    definition = "Наблюдаемое поведение, подтверждающее владение компетенцией"

    @factory.post_generation
    def scale(obj, create, extracted, **kwargs):  # noqa: N805
        """Создаёт полную шкалу уровней, если не указано иное."""
        if not create or extracted is False:
            return
        for level in range(1, 6):
            CompetencyScaleLevel.objects.create(
                competency=obj,
                level=level,
                name=f"Уровень {level}",
                indicators=f"Поведенческие индикаторы уровня {level}",
            )


class CompetencyModelFactory(DjangoModelFactory):
    """Модель компетенций для должности."""

    class Meta:
        model = CompetencyModel
        django_get_or_create = ("code",)

    code = factory.Sequence(lambda n: f"competency-model-{n}")
    name = factory.Sequence(lambda n: f"Модель компетенций {n}")
    version = 1
    is_published = True


class CompetencyRequirementFactory(DjangoModelFactory):
    """Требование модели компетенций: целевой уровень и вес."""

    class Meta:
        model = CompetencyRequirement

    competency_model = factory.SubFactory(CompetencyModelFactory)
    competency = factory.SubFactory(CompetencyFactory)
    target_level = 4
    weight = Decimal("1.000")


# ---------------------------------------------------------------------------
# Оценка
# ---------------------------------------------------------------------------
class AssessmentCampaignFactory(DjangoModelFactory):
    """Кампания оценки, открытая для внесения результатов."""

    class Meta:
        model = AssessmentCampaign
        django_get_or_create = ("code",)

    code = factory.Sequence(lambda n: f"campaign-{n}")
    name = factory.Sequence(lambda n: f"Оценка компетенций {n}")
    competency_model = factory.SubFactory(CompetencyModelFactory)
    status = CampaignStatus.ACTIVE
    period_start = factory.LazyFunction(lambda: TODAY() - dt.timedelta(days=30))
    period_end = factory.LazyFunction(lambda: TODAY() + dt.timedelta(days=30))
    deadline = factory.LazyFunction(lambda: TODAY() + dt.timedelta(days=14))


class AssessmentSessionFactory(DjangoModelFactory):
    """Сессия оценки, результаты которой учитываются в расчётах."""

    class Meta:
        model = AssessmentSession

    employee = factory.SubFactory(EmployeeFactory)
    assessor = factory.SubFactory(UserFactory)
    competency_model = factory.SubFactory(CompetencyModelFactory)
    method = AssessmentMethod.MANAGER
    status = SessionStatus.APPROVED
    assessment_date = factory.LazyFunction(TODAY)


class AssessmentScoreFactory(DjangoModelFactory):
    """Балл по компетенции внутри сессии."""

    class Meta:
        model = AssessmentScore

    session = factory.SubFactory(AssessmentSessionFactory)
    competency = factory.SubFactory(CompetencyFactory)
    level = Decimal("3.0")


# ---------------------------------------------------------------------------
# Кадровый резерв
# ---------------------------------------------------------------------------
class ReserveGroupFactory(DjangoModelFactory):
    """Группа подготовки резерва."""

    class Meta:
        model = ReserveGroup
        django_get_or_create = ("code",)

    code = factory.Sequence(lambda n: f"reserve-group-{n}")
    name = factory.Sequence(lambda n: f"Оперативный резерв {n}")
    kind = ReserveGroupKind.OPERATIONAL
    horizon_months = 12


class ReserveEntryFactory(DjangoModelFactory):
    """Действующая запись кадрового резерва."""

    class Meta:
        model = ReserveEntry

    employee = factory.SubFactory(EmployeeFactory)
    target_position = factory.SubFactory(PositionFactory)
    status = ReserveStatus.ACTIVE
    readiness_level = ReadinessLevel.MEDIUM
    readiness_index = Decimal("62.50")
    inclusion_date = factory.LazyFunction(lambda: TODAY() - dt.timedelta(days=180))
    review_date = factory.LazyFunction(lambda: TODAY() + dt.timedelta(days=185))


class ReserveNominationFactory(DjangoModelFactory):
    """Заявка на выдвижение, ожидающая решения."""

    class Meta:
        model = ReserveNomination

    employee = factory.SubFactory(EmployeeFactory)
    target_position = factory.SubFactory(PositionFactory)
    nominated_by = factory.SubFactory(UserFactory)
    justification = "Кандидат два года руководит проектной группой и закрыл план развития"
    status = "submitted"
    submitted_at = factory.LazyFunction(timezone.now)


# ---------------------------------------------------------------------------
# Преемственность
# ---------------------------------------------------------------------------
class KeyPositionFactory(DjangoModelFactory):
    """Критичная должность с высоким приоритетом планирования."""

    class Meta:
        model = KeyPosition

    position = factory.SubFactory(PositionFactory)
    criticality = CriticalityLevel.HIGH
    vacancy_risk = VacancyRisk.MEDIUM
    required_successors = 2


class SuccessionPlanFactory(DjangoModelFactory):
    """Действующий план преемственности."""

    class Meta:
        model = SuccessionPlan

    key_position = factory.SubFactory(KeyPositionFactory)
    title = factory.Sequence(lambda n: f"План преемственности {n}")
    status = SuccessionPlanStatus.ACTIVE
    horizon_months = 36


class SuccessionSlateFactory(DjangoModelFactory):
    """Преемник в плане."""

    class Meta:
        model = SuccessionSlate

    plan = factory.SubFactory(SuccessionPlanFactory)
    employee = factory.SubFactory(EmployeeFactory)
    rank = factory.Sequence(lambda n: n + 1)
    readiness_level = ReadinessLevel.MEDIUM


class AppointmentFactory(DjangoModelFactory):
    """Назначение сотрудника из кадрового резерва."""

    class Meta:
        model = Appointment

    employee = factory.SubFactory(EmployeeFactory)
    position = factory.SubFactory(PositionFactory)
    kind = AppointmentKind.FROM_RESERVE
    appointment_date = factory.LazyFunction(TODAY)


# ---------------------------------------------------------------------------
# Развитие
# ---------------------------------------------------------------------------
class DevelopmentPlanFactory(DjangoModelFactory):
    """Выполняемый индивидуальный план развития."""

    class Meta:
        model = DevelopmentPlan

    employee = factory.SubFactory(EmployeeFactory)
    title = factory.Sequence(lambda n: f"План развития {n}")
    status = PlanStatus.ACTIVE
    start_date = factory.LazyFunction(lambda: TODAY() - dt.timedelta(days=60))
    end_date = factory.LazyFunction(lambda: TODAY() + dt.timedelta(days=300))


class DevelopmentActivityFactory(DjangoModelFactory):
    """Запланированное мероприятие плана развития."""

    class Meta:
        model = DevelopmentActivity

    plan = factory.SubFactory(DevelopmentPlanFactory)
    title = factory.Sequence(lambda n: f"Мероприятие развития {n}")
    activity_type = ActivityType.TRAINING
    status = ActivityStatus.PENDING
    due_date = factory.LazyFunction(lambda: TODAY() + dt.timedelta(days=30))
    weight = 1


class MentorshipFactory(DjangoModelFactory):
    """Действующая наставническая пара."""

    class Meta:
        model = Mentorship

    mentor = factory.SubFactory(UserFactory, role="mentor")
    mentee = factory.SubFactory(EmployeeFactory)
    status = MentorshipStatus.ACTIVE
    goal = "Подготовка к руководству командой из пяти инженеров"


class LearningResourceFactory(DjangoModelFactory):
    """Учебный ресурс библиотеки развития."""

    class Meta:
        model = LearningResource
        django_get_or_create = ("code",)

    code = factory.Sequence(lambda n: f"resource-{n}")
    name = factory.Sequence(lambda n: f"Курс управления командой {n}")
    provider = "Корпоративный университет"


# ---------------------------------------------------------------------------
# Аналитика
# ---------------------------------------------------------------------------
class ScoringCriterionFactory(DjangoModelFactory):
    """Частный критерий индекса готовности."""

    class Meta:
        model = ScoringCriterion
        django_get_or_create = ("code",)

    code = factory.Sequence(lambda n: f"criterion-{n}")
    name = factory.Sequence(lambda n: f"Критерий готовности {n}")
    source = "competency_fit"


class ScoringSchemeFactory(DjangoModelFactory):
    """Схема оценивания с экспертными весами."""

    class Meta:
        model = ScoringScheme
        django_get_or_create = ("code",)

    code = factory.Sequence(lambda n: f"scheme-{n}")
    name = factory.Sequence(lambda n: f"Экспертная схема {n}")
    method = WeightingMethod.EXPERT


class ScoringWeightFactory(DjangoModelFactory):
    """Вес критерия в схеме оценивания."""

    class Meta:
        model = ScoringWeight

    scheme = factory.SubFactory(ScoringSchemeFactory)
    criterion = factory.SubFactory(ScoringCriterionFactory)
    weight = Decimal("1.00000")


class ReadinessSnapshotFactory(DjangoModelFactory):
    """Результат расчёта индекса готовности."""

    class Meta:
        model = ReadinessSnapshot

    entry = factory.SubFactory(ReserveEntryFactory)
    scheme = factory.SubFactory(ScoringSchemeFactory)
    index_value = Decimal("62.50")
    readiness_level = ReadinessLevel.MEDIUM
    criterion_values = factory.LazyFunction(
        lambda: {"competency_fit": {"raw": 3.5, "normalized": 62.5}}
    )


class CalibrationSessionFactory(DjangoModelFactory):
    """Сессия калибровки оценок результативности и потенциала."""

    class Meta:
        model = CalibrationSession

    name = factory.Sequence(lambda n: f"Калибровка оценок {n}")
    held_on = factory.LazyFunction(lambda: dt.date(2026, 3, 12))
    status = CalibrationStatus.IN_PROGRESS


class NineBoxPlacementFactory(DjangoModelFactory):
    """Размещение сотрудника в матрице «результативность — потенциал»."""

    class Meta:
        model = NineBoxPlacement

    employee = factory.SubFactory(EmployeeFactory)
    performance = Decimal("80.00")
    potential = Decimal("80.00")


__all__ = [
    "AppointmentFactory",
    "AssessmentCampaignFactory",
    "AssessmentScoreFactory",
    "AssessmentSessionFactory",
    "CalibrationSessionFactory",
    "CompetencyCategoryFactory",
    "CompetencyFactory",
    "CompetencyModelFactory",
    "CompetencyRequirementFactory",
    "DevelopmentActivityFactory",
    "DevelopmentPlanFactory",
    "EmployeeFactory",
    "EmploymentHistoryFactory",
    "KeyPositionFactory",
    "LearningResourceFactory",
    "MentorshipFactory",
    "NineBoxPlacementFactory",
    "ReadinessSnapshotFactory",
    "ReserveEntryFactory",
    "ReserveGroupFactory",
    "ReserveNominationFactory",
    "ScoringCriterionFactory",
    "ScoringSchemeFactory",
    "ScoringWeightFactory",
    "SuccessionPlanFactory",
    "SuccessionSlateFactory",
]
