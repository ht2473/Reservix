"""Методы планирования преемственности."""

import uuid

from ninja import Query, Router, Status
from ninja.pagination import paginate

from django.http import HttpRequest

from apps.api.auth import current_user
from apps.api.schemas import (
    AppointmentSchema,
    AppointmentStatisticsSchema,
    CoverageReportSchema,
    ErrorSchema,
    KeyPositionSchema,
    SuccessionPlanSchema,
)
from apps.succession.models import Appointment, KeyPosition, SuccessionPlan
from apps.succession.services import (
    appointment_statistics,
    coverage_report,
    uncovered_positions,
)
from core.constants import Role
from core.permissions import require_role

router = Router(tags=["Преемственность"])


@router.get(
    "/key-positions",
    response=list[KeyPositionSchema],
    summary="Критичные должности",
    description=(
        "Должности, освобождение которых создаёт риск для деятельности. "
        "Сортировка по приоритету планирования — произведению критичности "
        "на риск освобождения: именно в этом порядке кадровая служба "
        "распределяет усилия."
    ),
)
@paginate
def list_key_positions(
    request: HttpRequest,
    uncovered: bool = Query(default=False, description="Только без плана преемственности"),
    branch: int | None = Query(default=None),
):
    """Возвращает критичные должности с показателем обеспеченности."""
    # Отбор незакрытых должностей ведётся по отсутствию действующего плана, а не плана вообще.
    queryset = uncovered_positions() if uncovered else KeyPosition.objects.with_related().active()
    if branch is not None:
        queryset = queryset.filter(position__department__branch_id=branch)
    return queryset


@router.get(
    "/plans",
    response=list[SuccessionPlanSchema],
    summary="Планы преемственности",
    description=(
        "Планы замещения критичных должностей со списками преемников. "
        "Доступны кадровой службе и администратору: план содержит суждения о "
        "готовности конкретных сотрудников заменить действующего руководителя."
    ),
)
@paginate
def list_plans(
    request: HttpRequest,
    status: str | None = Query(default=None),
    key_position: int | None = Query(default=None),
):
    """Возвращает планы преемственности."""
    require_role(current_user(request), Role.ADMIN, Role.HR)

    queryset = SuccessionPlan.objects.with_related().prefetch_related("slate__employee")
    if status:
        queryset = queryset.filter(status=status)
    if key_position is not None:
        queryset = queryset.filter(key_position_id=key_position)
    return queryset


@router.get(
    "/plans/{uuid:public_id}",
    response={200: SuccessionPlanSchema, 404: ErrorSchema},
    summary="План преемственности",
)
def plan_detail(request: HttpRequest, public_id: uuid.UUID):
    """Возвращает план преемственности со списком преемников."""
    require_role(current_user(request), Role.ADMIN, Role.HR)

    plan = (
        SuccessionPlan.objects.with_related()
        .prefetch_related("slate__employee")
        .filter(public_id=public_id)
        .first()
    )
    if plan is None:
        return Status(404, {"code": "not_found", "detail": "План преемственности не найден"})
    return Status(200, plan)


@router.get(
    "/appointments",
    response=list[AppointmentSchema],
    summary="Назначения",
    description=(
        "Факты назначения на должности. Источник главной метрики системы — "
        "доли назначений из кадрового резерва относительно всех назначений "
        "на критичные должности."
    ),
)
@paginate
def list_appointments(
    request: HttpRequest,
    year: int | None = Query(default=None),
    kind: str | None = Query(default=None),
    key_only: bool = Query(default=False),
):
    """Возвращает назначения с отбором по году и источнику."""
    queryset = Appointment.objects.with_related()
    if year is not None:
        queryset = queryset.in_year(year)
    if kind:
        queryset = queryset.filter(kind=kind)
    if key_only:
        queryset = queryset.on_key_positions()
    return queryset


@router.get(
    "/coverage",
    response=CoverageReportSchema,
    summary="Обеспеченность критичных должностей",
    description=(
        "Три показателя, отвечающие на три разных вопроса и намеренно не "
        "сводимые в один. Покрытие говорит, где вообще нет преемников; "
        "глубина скамейки — хватит ли запаса при отказе первого кандидата; "
        "немедленная готовность — что произойдёт, если уход случится завтра. "
        "Организация с полным покрытием и нулевой немедленной готовностью "
        "выглядит благополучно ровно до первого заявления об увольнении."
    ),
)
def coverage(request: HttpRequest):
    """Возвращает сводку обеспеченности критичных должностей преемниками."""
    require_role(current_user(request), Role.ADMIN, Role.HR, Role.MANAGER)
    return coverage_report()


@router.get(
    "/statistics",
    response=AppointmentStatisticsSchema,
    summary="Результативность системы",
    description=(
        "Доля назначений из кадрового резерва и средний срок подготовки. "
        "Первый показатель — главный для всей системы: он отвечает на вопрос, "
        "закрываются ли ключевые должности подготовленными людьми или наймом "
        "с рынка. Численность резерва сама по себе на этот вопрос не отвечает."
    ),
)
def statistics(
    request: HttpRequest,
    year: int | None = Query(default=None, description="Календарный год"),
    months: int | None = Query(default=None, description="Глубина окна в месяцах"),
):
    """Возвращает показатели результативности за период."""
    require_role(current_user(request), Role.ADMIN, Role.HR, Role.MANAGER)
    return appointment_statistics(year=year, months=months)
