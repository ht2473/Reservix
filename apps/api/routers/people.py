"""Методы кадровых данных."""

import uuid

from ninja import Query, Router, Status
from ninja.pagination import paginate

from django.db.models import Q, QuerySet
from django.http import HttpRequest

from apps.api.auth import current_user
from apps.api.schemas import (
    CompetencyProfileItemSchema,
    EmployeeDetailSchema,
    EmployeeFilterSchema,
    EmployeeListSchema,
    EmploymentHistorySchema,
    ErrorSchema,
)
from apps.assessment.models import AssessmentScore
from apps.competencies.models import CompetencyModel
from apps.people.models import Employee
from core.permissions import apply_employee_scope, can_view_employee

router = Router(tags=["Сотрудники"])


def visible_employees(request: HttpRequest) -> QuerySet[Employee]:
    """Набор сотрудников, доступных вызывающему по области видимости."""
    return apply_employee_scope(Employee.objects.with_org(), current_user(request))


@router.get(
    "/employees",
    response=list[EmployeeListSchema],
    summary="Сотрудники",
    description=(
        "Список сотрудников в границах области видимости вызывающего. "
        "Руководитель видит своё подразделение с вложенными, кадровая служба — "
        "организацию, рядовой сотрудник — только собственную карточку."
    ),
)
@paginate
def list_employees(
    request: HttpRequest,
    filters: EmployeeFilterSchema = Query(...),
    search: str = Query(default="", description="Поиск по ФИО, табельному номеру и почте"),
    ordering: str = Query(default="last_name", description="Поле сортировки"),
):
    """Возвращает сотрудников с отбором, поиском и постраничной навигацией."""
    employees = Employee.objects.with_org()
    if search:
        employees = employees.search(search)
    queryset = filters.filter(apply_employee_scope(employees, current_user(request)))

    allowed_ordering = {
        "last_name",
        "-last_name",
        "hire_date",
        "-hire_date",
        "personnel_number",
        "-personnel_number",
    }
    if ordering in allowed_ordering:
        queryset = queryset.order_by(ordering)

    return queryset


@router.get(
    "/employees/{uuid:public_id}",
    response={200: EmployeeDetailSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Карточка сотрудника",
    description=(
        "Сведения о сотруднике в объёме, допустимом для программного "
        "интерфейса: без даты рождения и личных контактов."
    ),
)
def employee_detail(request: HttpRequest, public_id: uuid.UUID):
    """Возвращает карточку сотрудника с проверкой права на объект."""
    employee = Employee.objects.with_org().filter(public_id=public_id).first()
    if employee is None:
        return Status(404, {"code": "not_found", "detail": "Сотрудник не найден"})
    if not can_view_employee(current_user(request), employee):
        return Status(
            403,
            {
                "code": "forbidden",
                "detail": "Карточка находится вне вашей области видимости",
            },
        )
    return Status(200, employee)


@router.get(
    "/employees/{uuid:public_id}/history",
    response={200: list[EmploymentHistorySchema], 403: ErrorSchema, 404: ErrorSchema},
    summary="Кадровая история",
    description=(
        "Назначения сотрудника в обратном хронологическом порядке. История — "
        "источник критерия «динамика карьеры» в индексе готовности."
    ),
)
def employee_history(request: HttpRequest, public_id: uuid.UUID):
    """Возвращает кадровую историю сотрудника."""
    employee = Employee.objects.filter(public_id=public_id).first()
    if employee is None:
        return Status(404, {"code": "not_found", "detail": "Сотрудник не найден"})
    if not can_view_employee(current_user(request), employee):
        return Status(
            403,
            {
                "code": "forbidden",
                "detail": "Карточка находится вне вашей области видимости",
            },
        )
    return Status(200, list(employee.employment_history.select_related("position", "department")))


@router.get(
    "/employees/{uuid:public_id}/competencies",
    response={200: list[CompetencyProfileItemSchema], 403: ErrorSchema, 404: ErrorSchema},
    summary="Профиль компетенций",
    description=(
        "Фактические уровни владения компетенциями в сопоставлении с "
        "требованиями модели компетенций целевой должности. Разрыв "
        "рассчитывается по последней утверждённой оценке каждой компетенции: "
        "более ранние оценки характеризуют сотрудника, каким он был, а не есть."
    ),
)
def employee_competencies(
    request: HttpRequest,
    public_id: uuid.UUID,
    model_id: int | None = Query(default=None, description="Модель компетенций для сравнения"),
):
    """Строит профиль компетенций сотрудника с разрывами относительно модели."""
    employee = Employee.objects.filter(public_id=public_id).first()
    if employee is None:
        return Status(404, {"code": "not_found", "detail": "Сотрудник не найден"})
    if not can_view_employee(current_user(request), employee):
        return Status(
            403,
            {
                "code": "forbidden",
                "detail": "Карточка находится вне вашей области видимости",
            },
        )

    # Последняя утверждённая оценка по каждой компетенции.
    actual: dict[int, float] = {}
    category_names: dict[int, str] = {}
    competency_names: dict[int, str] = {}

    scores = (
        AssessmentScore.objects.counted()
        .filter(session__employee=employee)
        .select_related("competency", "competency__category")
        .order_by("session__assessment_date")
    )
    for score in scores:
        actual[score.competency_id] = float(score.level)
        competency_names[score.competency_id] = score.competency.name
        category_names[score.competency_id] = score.competency.category.name

    model = None
    if model_id is not None:
        model = CompetencyModel.objects.filter(pk=model_id).first()
    elif employee.position_id:
        model = CompetencyModel.objects.for_position(employee.position_id).first()

    result: list[dict[str, object]] = []
    covered: set[int] = set()

    if model is not None:
        for requirement in model.requirements.with_competency():
            competency = requirement.competency
            covered.add(competency.pk)
            level = actual.get(competency.pk)
            result.append(
                {
                    "competency_id": competency.pk,
                    "competency_name": competency.name,
                    "category_name": competency.category.name,
                    "actual_level": level,
                    "target_level": requirement.target_level,
                    "gap": requirement.gap_for(level),
                    "is_critical": requirement.is_critical,
                }
            )

    # Оценки по компетенциям вне модели тоже показываются: сотрудник мог быть оценён по прежней
    # модели, и скрывать эти сведения значило бы потерять часть истории оценки.
    for competency_id, level in actual.items():
        if competency_id in covered:
            continue
        result.append(
            {
                "competency_id": competency_id,
                "competency_name": competency_names[competency_id],
                "category_name": category_names[competency_id],
                "actual_level": level,
                "target_level": None,
                "gap": 0.0,
                "is_critical": False,
            }
        )

    return Status(200, result)


@router.get(
    "/employees/{uuid:public_id}/subordinates",
    response={200: list[EmployeeListSchema], 403: ErrorSchema, 404: ErrorSchema},
    summary="Подчинённые",
    description="Сотрудники, для которых указанный сотрудник — непосредственный руководитель.",
)
def employee_subordinates(request: HttpRequest, public_id: uuid.UUID):
    """Возвращает непосредственных подчинённых сотрудника."""
    employee = Employee.objects.filter(public_id=public_id).first()
    if employee is None:
        return Status(404, {"code": "not_found", "detail": "Сотрудник не найден"})
    if not can_view_employee(current_user(request), employee):
        return Status(
            403,
            {
                "code": "forbidden",
                "detail": "Карточка находится вне вашей области видимости",
            },
        )

    subordinates = (
        Employee.objects.with_org().filter(manager=employee).exclude(Q(status="dismissed"))
    )
    return Status(200, list(subordinates))
