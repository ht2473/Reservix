"""Схемы данных программного интерфейса."""

import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated, Any

from ninja import Field, FilterSchema, Schema
from ninja.filter_schema import FilterLookup


# ---------------------------------------------------------------------------
# Служебные схемы
# ---------------------------------------------------------------------------
class ErrorSchema(Schema):
    """Единый формат ошибки: код, сообщение и, при наличии, разбор по полям."""

    code: str = Field(description="Машиночитаемый код ошибки")
    detail: str = Field(description="Пояснение для человека")
    fields: dict[str, list[str]] | None = Field(
        default=None, description="Ошибки по отдельным полям запроса"
    )


class HealthSchema(Schema):
    """Состояние службы."""

    status: str
    version: str
    database: bool
    server_time: dt.datetime


class CurrentUserSchema(Schema):
    """Сведения о текущем пользователе и его правах."""

    id: int
    email: str
    full_name: str
    role: str
    role_label: str
    scope_level: str
    branch_id: int | None
    department_id: int | None
    is_department_head: bool
    employee_public_id: uuid.UUID | None


class CountSchema(Schema):
    """Результат операции, возвращающей число затронутых записей."""

    count: int


# ---------------------------------------------------------------------------
# Организационная структура
# ---------------------------------------------------------------------------
class BranchSchema(Schema):
    """Филиал организации."""

    id: int
    code: str
    name: str
    city: str
    timezone: str
    is_head_office: bool
    is_active: bool


class DepartmentSchema(Schema):
    """Подразделение с указанием положения в иерархии."""

    id: int
    code: str
    name: str
    branch_id: int
    parent_id: int | None
    depth: int
    path: str
    is_active: bool


class DepartmentTreeSchema(Schema):
    """Узел дерева подразделений."""

    id: int
    code: str
    name: str
    branch_id: int
    depth: int
    headcount: int
    children: list["DepartmentTreeSchema"] = Field(default_factory=list)


# Ссылка схемы на саму себя разрешается после объявления класса: до этого
# момента имя ещё не связано, и построение модели завершилось бы ошибкой.
DepartmentTreeSchema.model_rebuild()


class PositionGradeSchema(Schema):
    """Грейд должности."""

    id: int
    code: str
    name: str
    level: int
    is_managerial: bool


class PositionSchema(Schema):
    """Должность штатного расписания."""

    id: int
    code: str
    name: str
    grade_id: int
    grade_level: int = Field(..., alias="grade.level")
    grade_name: str = Field(..., alias="grade.name")
    department_id: int | None
    headcount_plan: int
    is_key_position: bool
    is_active: bool


class PositionProfileSchema(Schema):
    """Профиль требований к должности."""

    id: int
    position_id: int
    purpose: str
    min_education: str
    min_total_experience_years: int
    min_relevant_experience_years: int
    min_company_tenure_months: int
    requires_management_experience: bool
    min_subordinates: int
    is_published: bool


# ---------------------------------------------------------------------------
# Сотрудники
# ---------------------------------------------------------------------------
class EmployeeListSchema(Schema):
    """Сотрудник в списке: минимум сведений, достаточный для выбора."""

    public_id: uuid.UUID
    personnel_number: str
    short_name: str
    title: str
    branch_id: int | None
    department_id: int | None
    position_id: int | None
    status: str
    is_key_talent: bool
    is_external: bool


class EmployeeDetailSchema(Schema):
    """Карточка сотрудника в объёме, допустимом для программного интерфейса."""

    public_id: uuid.UUID
    personnel_number: str
    last_name: str
    first_name: str
    middle_name: str
    full_name: str
    email: str
    branch_id: int | None
    department_id: int | None
    position_id: int | None
    title: str
    status: str
    hire_date: dt.date
    tenure_months: int
    position_tenure_months: int
    total_experience_months: int
    management_experience_months: int
    max_subordinates: int
    education_level: str
    mobility: str
    attrition_risk: str
    is_key_talent: bool
    is_external: bool
    manager_id: int | None


class EmployeeFilterSchema(FilterSchema):
    """Условия отбора сотрудников."""

    branch: Annotated[int | None, FilterLookup("branch_id")] = None
    department: Annotated[int | None, FilterLookup("department_id")] = None
    position: Annotated[int | None, FilterLookup("position_id")] = None
    status: Annotated[str | None, FilterLookup("status")] = None
    key_talent: Annotated[bool | None, FilterLookup("is_key_talent")] = None
    external: Annotated[bool | None, FilterLookup("is_external")] = None
    risk: Annotated[str | None, FilterLookup("attrition_risk")] = None


class EmploymentHistorySchema(Schema):
    """Запись кадровой истории."""

    id: int
    position_title: str
    position_id: int | None
    department_id: int | None
    grade_level: int | None
    change_type: str
    start_date: dt.date
    end_date: dt.date | None
    duration_months: int
    subordinates_count: int


# ---------------------------------------------------------------------------
# Компетенции
# ---------------------------------------------------------------------------
class CompetencyCategorySchema(Schema):
    """Категория справочника компетенций."""

    id: int
    code: str
    name: str
    kind: str
    is_active: bool


class CompetencySchema(Schema):
    """Компетенция справочника."""

    id: int
    code: str
    name: str
    category_id: int
    kind: str
    definition: str
    is_testable: bool
    is_core: bool
    is_active: bool


class CompetencyRequirementSchema(Schema):
    """Требование модели компетенций."""

    id: int
    competency_id: int
    competency_name: str = Field(..., alias="competency.name")
    target_level: int
    minimum_level: int | None
    weight: Decimal
    is_critical: bool


class CompetencyModelSchema(Schema):
    """Модель компетенций с перечнем требований."""

    id: int
    code: str
    name: str
    version: int
    grade_id: int | None
    is_published: bool
    is_active: bool
    requirements: list[CompetencyRequirementSchema] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Оценка
# ---------------------------------------------------------------------------
class AssessmentCampaignSchema(Schema):
    """Кампания оценки."""

    public_id: uuid.UUID
    code: str
    name: str
    status: str
    competency_model_id: int
    period_start: dt.date
    period_end: dt.date
    deadline: dt.date
    is_360_enabled: bool
    completion_rate: float


class AssessmentScoreSchema(Schema):
    """Балл по компетенции."""

    id: int
    competency_id: int
    competency_name: str = Field(..., alias="competency.name")
    level: Decimal
    raw_score: Decimal | None
    comment: str


class AssessmentSessionSchema(Schema):
    """Сессия оценки с баллами по компетенциям."""

    public_id: uuid.UUID
    employee_id: int
    assessor_id: int | None
    campaign_id: int | None
    competency_model_id: int
    method: str
    status: str
    assessment_date: dt.date
    average_level: float | None
    summary: str
    scores: list[AssessmentScoreSchema] = Field(default_factory=list)


class CompetencyProfileItemSchema(Schema):
    """Строка профиля компетенций сотрудника."""

    competency_id: int
    competency_name: str
    category_name: str
    actual_level: float | None
    target_level: int | None
    gap: float
    is_critical: bool


# ---------------------------------------------------------------------------
# Кадровый резерв
# ---------------------------------------------------------------------------
class ReserveGroupSchema(Schema):
    """Группа резерва."""

    id: int
    code: str
    name: str
    kind: str
    horizon_months: int
    member_count: int
    capacity: int | None
    is_active: bool


class ReserveEntrySchema(Schema):
    """Запись кадрового резерва."""

    public_id: uuid.UUID
    employee_id: int
    employee_name: str = Field(..., alias="employee.short_name")
    target_position_id: int
    target_position_name: str = Field(..., alias="target_position.name")
    reserve_group_id: int | None
    status: str
    readiness_level: str
    readiness_index: Decimal | None
    rank: int | None
    inclusion_date: dt.date
    review_date: dt.date | None
    months_in_reserve: int


class ReserveEntryFilterSchema(FilterSchema):
    """Условия отбора записей резерва."""

    status: Annotated[str | None, FilterLookup("status")] = None
    readiness: Annotated[str | None, FilterLookup("readiness_level")] = None
    position: Annotated[int | None, FilterLookup("target_position_id")] = None
    group: Annotated[int | None, FilterLookup("reserve_group_id")] = None
    branch: Annotated[int | None, FilterLookup("employee__branch_id")] = None
    department: Annotated[int | None, FilterLookup("employee__department_id")] = None


class ReserveStatusHistorySchema(Schema):
    """Изменение статуса записи резерва."""

    id: int
    old_status: str
    new_status: str
    changed_by_id: int | None
    reason: str
    changed_at: dt.datetime


class ReserveTransitionSchema(Schema):
    """Запрос на смену статуса записи резерва."""

    status: str = Field(description="Целевой статус записи")
    reason: str = Field(default="", max_length=1000, description="Основание перехода")


class NominationSchema(Schema):
    """Заявка на выдвижение в резерв."""

    public_id: uuid.UUID
    employee_id: int
    employee_name: str = Field(..., alias="employee.short_name")
    target_position_id: int
    target_position_name: str = Field(..., alias="target_position.name")
    kind: str
    status: str
    nominated_by_id: int | None
    justification: str
    submitted_at: dt.datetime | None
    waiting_days: int


class NominationCreateSchema(Schema):
    """Данные для подачи заявки на выдвижение."""

    employee_id: int
    target_position_id: int
    reserve_group_id: int | None = None
    kind: str = "manager"
    justification: str = Field(min_length=30, max_length=4000)
    achievements: str = Field(default="", max_length=4000)


# ---------------------------------------------------------------------------
# Преемственность
# ---------------------------------------------------------------------------
class KeyPositionSchema(Schema):
    """Критичная должность."""

    id: int
    position_id: int
    position_name: str = Field(..., alias="position.name")
    incumbent_id: int | None
    criticality: int
    vacancy_risk: int
    priority_score: int
    required_successors: int
    successor_count: int
    coverage: float
    is_active: bool


class SuccessionSlateSchema(Schema):
    """Преемник в плане."""

    id: int
    employee_id: int
    employee_name: str = Field(..., alias="employee.short_name")
    rank: int
    readiness_level: str
    readiness_index: Decimal | None
    is_emergency_successor: bool


class SuccessionPlanSchema(Schema):
    """План преемственности со списком преемников."""

    public_id: uuid.UUID
    key_position_id: int
    title: str
    status: str
    horizon_months: int
    start_date: dt.date
    review_date: dt.date | None
    successor_count: int
    ready_now_count: int
    slate: list[SuccessionSlateSchema] = Field(default_factory=list)


class NominationDecisionSchema(Schema):
    """Решение согласующего по заявке."""

    approve: bool = Field(description="Согласовать либо отклонить")
    comment: str = Field(default="", max_length=1000, description="Основание решения")
    reserve_group_id: int | None = Field(
        default=None, description="Программа подготовки; учитывается на ступени кадровой службы"
    )


class CoverageReportSchema(Schema):
    """Обеспеченность критичных должностей преемниками."""

    total: int = Field(description="Критичных должностей в планировании")
    covered: int = Field(description="Должностей хотя бы с одним преемником")
    with_ready: int = Field(description="Должностей с преемником, готовым к назначению")
    with_emergency: int = Field(description="Должностей с кандидатом на немедленный ввод")
    successors: int = Field(description="Всего преемников в действующих планах")
    coverage: float = Field(description="Покрытие, проценты")
    ready_coverage: float = Field(description="Немедленная готовность, проценты")
    bench_depth: float = Field(description="Преемников на должность")


class AppointmentStatisticsSchema(Schema):
    """Результативность системы: доля назначений из резерва и срок подготовки."""

    total: int
    from_reserve: int
    reserve_share: float = Field(description="Доля назначений из резерва, проценты")
    on_key_positions: int
    average_months: float | None = Field(description="Средний срок подготовки, месяцев")
    average_index: float | None = Field(description="Средний индекс готовности при назначении")


class AppointmentSchema(Schema):
    """Назначение на должность."""

    public_id: uuid.UUID
    employee_id: int
    position_id: int
    kind: str
    appointment_date: dt.date
    order_number: str
    months_in_reserve: int | None
    readiness_index_at_appointment: Decimal | None


# ---------------------------------------------------------------------------
# Развитие
# ---------------------------------------------------------------------------
class DevelopmentActivitySchema(Schema):
    """Мероприятие плана развития."""

    public_id: uuid.UUID
    title: str
    activity_type: str
    status: str
    competency_id: int | None
    start_date: dt.date | None
    due_date: dt.date
    completion_date: dt.date | None
    mentor_id: int | None
    weight: int
    is_overdue: bool


class DevelopmentPlanSchema(Schema):
    """Индивидуальный план развития."""

    public_id: uuid.UUID
    employee_id: int
    entry_id: int | None
    title: str
    status: str
    start_date: dt.date
    end_date: dt.date
    completion_rate: int
    activity_count: int
    overdue_count: int


class DevelopmentPlanDetailSchema(DevelopmentPlanSchema):
    """План развития вместе с мероприятиями."""

    goal: str
    activities: list[DevelopmentActivitySchema] = Field(default_factory=list)


class MentorshipSchema(Schema):
    """Наставническая пара."""

    public_id: uuid.UUID
    mentor_id: int
    mentee_id: int
    status: str
    goal: str
    start_date: dt.date
    end_date: dt.date | None
    meetings_count: int
    is_meeting_due: bool


# ---------------------------------------------------------------------------
# Аналитика
# ---------------------------------------------------------------------------
class ScoringCriterionSchema(Schema):
    """Частный критерий готовности."""

    id: int
    code: str
    name: str
    source: str
    direction: str
    unit: str
    is_active: bool


class ScoringWeightSchema(Schema):
    """Вес критерия в схеме оценивания."""

    criterion_id: int
    criterion_code: str = Field(..., alias="criterion.code")
    criterion_name: str = Field(..., alias="criterion.name")
    weight: Decimal
    percent: float
    rank: int | None


class ScoringSchemeSchema(Schema):
    """Схема оценивания с весами критериев."""

    id: int
    code: str
    name: str
    method: str
    aggregation: str
    is_default: bool
    is_active: bool
    is_consistent: bool
    consistency_ratio: Decimal | None
    weights: list[ScoringWeightSchema] = Field(default_factory=list)


class ReadinessSnapshotSchema(Schema):
    """Результат расчёта индекса готовности."""

    id: int
    entry_id: int
    scheme_id: int
    index_value: Decimal
    readiness_level: str
    rank_in_position: int | None
    candidates_total: int | None
    calculated_at: dt.datetime
    #: Разбор индекса по критериям.
    criterion_values: dict[str, Any]


class NineBoxCellSchema(Schema):
    """Заполнение одной ячейки матрицы 9-box."""

    cell: str
    label: str
    count: int
    share: float


class NineBoxDistributionSchema(Schema):
    """Распределение сотрудников по матрице 9-box."""

    total: int
    cells: list[NineBoxCellSchema]


class CriterionContributionSchema(Schema):
    """Вклад одного частного критерия в композитный индекс."""

    code: str
    name: str
    value: float
    weight: float
    points: float
    has_data: bool
    note: str


class ReadinessBreakdownSchema(Schema):
    """Разбор индекса готовности одной записи резерва."""

    entry_id: int
    scheme: str
    aggregation: str
    index_value: float
    readiness_level: str
    coverage: float
    is_reliable: bool
    rank: int | None
    candidates_total: int | None
    contributions: list[CriterionContributionSchema]


class SchemeCorrelationSchema(Schema):
    """Согласие ранжирований двух схем оценивания."""

    left: str
    right: str
    correlation: float
    top_agreement: float


class SchemeComparisonSchema(Schema):
    """Сравнение ранжирований, полученных разными схемами весов."""

    entries: int
    top_size: int
    verdict: str
    min_correlation: float
    pairs: list[SchemeCorrelationSchema]


class StabilityCriterionSchema(Schema):
    """Чувствительность ранжирования к весу одного критерия."""

    code: str
    name: str
    worst_correlation: float
    worst_delta: float
    stable_share: float
    max_rank_shift: float


class StabilityReportSchema(Schema):
    """Устойчивость ранжирования к неточности весов."""

    scheme: str
    entries: int
    entries_total: int
    criteria: int
    worst_correlation: float
    average_stability: float
    is_robust: bool
    critical_criterion: str | None
    by_criterion: list[StabilityCriterionSchema]


class ReadinessForecastSchema(Schema):
    """Прогноз срока достижения готовности."""

    entry_id: int
    current_index: float
    target_index: float
    slope_per_month: float
    months_to_target: float | None
    expected_date: dt.date | None
    observations: int
    note: str


class ReserveMetricsSchema(Schema):
    """Показатели здоровья кадрового резерва."""

    employees_total: int
    reserve_entries: int
    reserve_share: float
    ready_now: int
    ready_share: float
    key_positions: int
    covered_key_positions: int
    coverage_share: float
    appointments_total: int
    appointments_from_reserve: int
    internal_appointment_share: float
    average_readiness_index: float | None


# ---------------------------------------------------------------------------
# Компетентностное соответствие
# ---------------------------------------------------------------------------
class RequirementGapSchema(Schema):
    """Разрыв по одному требованию модели компетенций."""

    competency_id: int
    competency_name: str
    category_name: str
    target_level: int
    actual_level: float | None
    gap: float
    weight: Decimal
    attainment: float
    is_critical: bool
    is_satisfied: bool
    is_blocking: bool
    confidence: float | None
    methods: int


class CompetencyProfileSchema(Schema):
    """Профиль компетенций сотрудника относительно модели требований."""

    employee_id: uuid.UUID
    competency_model_id: int | None
    competency_model_name: str | None
    match_score: float
    coverage: float
    confidence: float
    average_level: float | None
    average_target: float | None
    is_blocked: bool
    evaluated_on: dt.date
    gaps: list[RequirementGapSchema] = Field(default_factory=list)


class CompetencyScaleLevelSchema(Schema):
    """Уровень шкалы компетенции с поведенческими индикаторами."""

    level: int
    name: str
    indicators: str
