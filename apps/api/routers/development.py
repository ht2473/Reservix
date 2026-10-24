"""Методы индивидуальных планов развития и наставничества."""

import uuid

from ninja import Query, Router, Status
from ninja.pagination import paginate

from django.http import HttpRequest

from apps.api.auth import current_user
from apps.api.schemas import (
    DevelopmentActivitySchema,
    DevelopmentPlanDetailSchema,
    DevelopmentPlanSchema,
    ErrorSchema,
    MentorshipSchema,
)
from apps.development.models import DevelopmentActivity, DevelopmentPlan, Mentorship
from core.permissions import apply_scope_by_path

router = Router(tags=["Развитие"])


@router.get(
    "/plans",
    response=list[DevelopmentPlanSchema],
    summary="Планы развития",
    description=(
        "Индивидуальные планы развития в границах области видимости. Процент "
        "выполнения считается по мероприятиям с учётом их веса: обучение с "
        "защитой проекта и однодневный вебинар вносят разный вклад."
    ),
)
@paginate
def list_plans(
    request: HttpRequest,
    employee: int | None = Query(default=None),
    status: str | None = Query(default=None),
    overdue: bool = Query(default=False, description="Только планы с истёкшим сроком"),
):
    """Возвращает планы развития с отбором по сотруднику и состоянию."""
    queryset = apply_scope_by_path(
        DevelopmentPlan.objects.with_related(), current_user(request), prefix="employee"
    )
    if employee is not None:
        queryset = queryset.filter(employee_id=employee)
    if status:
        queryset = queryset.filter(status=status)
    if overdue:
        from django.utils import timezone

        queryset = queryset.filter(status="active", end_date__lt=timezone.localdate())
    return queryset


@router.get(
    "/plans/{uuid:public_id}",
    response={200: DevelopmentPlanDetailSchema, 404: ErrorSchema},
    summary="План развития",
    description="План вместе с мероприятиями, сроками и признаком просрочки.",
)
def plan_detail(request: HttpRequest, public_id: uuid.UUID):
    """Возвращает план развития с перечнем мероприятий."""
    queryset = apply_scope_by_path(
        DevelopmentPlan.objects.with_related(), current_user(request), prefix="employee"
    )
    plan = queryset.prefetch_related("activities").filter(public_id=public_id).first()
    if plan is None:
        return Status(404, {"code": "not_found", "detail": "План развития не найден"})
    return Status(200, plan)


@router.get(
    "/activities",
    response=list[DevelopmentActivitySchema],
    summary="Мероприятия развития",
    description=(
        "Мероприятия планов развития. Параметр `due_soon` оставляет "
        "приближающиеся по сроку — рабочий список для напоминаний, `overdue` — "
        "просроченные, требующие вмешательства куратора."
    ),
)
@paginate
def list_activities(
    request: HttpRequest,
    plan: int | None = Query(default=None),
    status: str | None = Query(default=None),
    activity_type: str | None = Query(default=None),
    due_soon: bool = Query(default=False),
    overdue: bool = Query(default=False),
):
    """Возвращает мероприятия развития с отбором по сроку и состоянию."""
    # Отбор по сроку выполняется на специализированном наборе запросов до сужения области
    # видимости: правила «приближается» и «просрочено» описаны в модели и не должны повторяться в
    # интерфейсе.
    activities = DevelopmentActivity.objects.with_related()
    if plan is not None:
        activities = activities.filter(plan_id=plan)
    if status:
        activities = activities.filter(status=status)
    if activity_type:
        activities = activities.filter(activity_type=activity_type)
    if due_soon:
        activities = activities.due_soon()
    if overdue:
        activities = activities.overdue()

    return apply_scope_by_path(activities, current_user(request), prefix="plan__employee")


@router.get(
    "/mentorships",
    response=list[MentorshipSchema],
    summary="Наставничество",
    description=(
        "Наставнические пары с состоянием и признаком наступившего срока очередной встречи."
    ),
)
@paginate
def list_mentorships(
    request: HttpRequest,
    mentor: int | None = Query(default=None),
    status: str | None = Query(default=None),
):
    """Возвращает наставнические пары."""
    queryset = apply_scope_by_path(
        Mentorship.objects.with_related(), current_user(request), prefix="mentee"
    )
    if mentor is not None:
        queryset = queryset.filter(mentor_id=mentor)
    if status:
        queryset = queryset.filter(status=status)
    return queryset
