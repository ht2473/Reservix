"""Методы оценки компетенций."""

import uuid

from ninja import Query, Router, Status
from ninja.pagination import paginate

from django.db.models import QuerySet
from django.http import HttpRequest

from apps.api.auth import current_user
from apps.api.schemas import (
    AssessmentCampaignSchema,
    AssessmentSessionSchema,
    ErrorSchema,
)
from apps.assessment.models import (
    AssessmentCampaign,
    AssessmentMethod,
    AssessmentSession,
)
from core.constants import Role
from core.permissions import apply_scope_by_path

router = Router(tags=["Оценка"])


@router.get(
    "/campaigns",
    response=list[AssessmentCampaignSchema],
    summary="Кампании оценки",
    description=(
        "Процедуры оценки с периодом, сроком и долей завершённых сессий. Доля "
        "завершённых — рабочий показатель организатора: она показывает, где "
        "процедура буксует, до истечения срока."
    ),
)
@paginate
def list_campaigns(
    request: HttpRequest,
    status: str | None = Query(default=None),
    year: int | None = Query(default=None),
):
    """Возвращает кампании оценки с отбором по состоянию и году."""
    queryset = AssessmentCampaign.objects.select_related("competency_model")
    if status:
        queryset = queryset.filter(status=status)
    if year is not None:
        queryset = queryset.for_year(year)
    return queryset


@router.get(
    "/sessions",
    response=list[AssessmentSessionSchema],
    summary="Сессии оценки",
    description=(
        "Акты оценивания с баллами по компетенциям. Сотрудник без "
        "управленческой роли видит только сессии, где оцениваемым является он "
        "сам либо оценивающим выступал он."
    ),
)
@paginate
def list_sessions(
    request: HttpRequest,
    employee: int | None = Query(default=None),
    campaign: int | None = Query(default=None),
    method: str | None = Query(default=None),
    status: str | None = Query(default=None),
):
    """Возвращает сессии оценки в границах прав вызывающего."""
    queryset = _visible_sessions(request)
    if employee is not None:
        queryset = queryset.filter(employee_id=employee)
    if campaign is not None:
        queryset = queryset.filter(campaign_id=campaign)
    if method:
        queryset = queryset.filter(method=method)
    if status:
        queryset = queryset.filter(status=status)
    return queryset


@router.get(
    "/sessions/{uuid:public_id}",
    response={200: AssessmentSessionSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Сессия оценки",
)
def session_detail(request: HttpRequest, public_id: uuid.UUID):
    """Возвращает сессию оценки вместе с баллами по компетенциям."""
    session = _visible_sessions(request).filter(public_id=public_id).first()
    if session is None:
        # Отсутствие права и отсутствие записи намеренно не различаются: ответ «нет прав» сам по
        # себе подтверждал бы существование сессии оценки конкретного сотрудника.
        return Status(404, {"code": "not_found", "detail": "Сессия оценки не найдена"})
    return Status(200, session)


def _visible_sessions(request: HttpRequest) -> QuerySet[AssessmentSession]:
    """Сессии оценки, доступные вызывающему."""
    user = current_user(request)
    queryset = (
        AssessmentSession.objects.select_related("employee", "assessor", "competency_model")
        .prefetch_related("scores__competency")
        .order_by("-assessment_date")
    )

    if user.role in {Role.ADMIN, Role.HR}:
        return apply_scope_by_path(queryset, user, prefix="employee")

    scoped = apply_scope_by_path(queryset, user, prefix="employee")
    own = queryset.filter(assessor=user)
    # Объединение выполняется по идентификаторам, а не через `|`: соединение
    # двух наборов с разными связями порождает дубликаты строк.
    identifiers = set(scoped.values_list("pk", flat=True)) | set(own.values_list("pk", flat=True))
    return queryset.filter(pk__in=identifiers)


@router.get(
    "/methods",
    response=list[dict[str, str]],
    summary="Методы оценки",
    description=(
        "Перечень методов оценки с наименованиями. Метод определяет, кто "
        "выставляет балл и как он проверяется."
    ),
)
def list_methods(request: HttpRequest) -> list[dict[str, str]]:
    """Возвращает справочник методов оценки."""
    return [{"value": value, "label": str(label)} for value, label in AssessmentMethod.choices]
