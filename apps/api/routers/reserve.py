"""Методы кадрового резерва."""

import uuid

from ninja import Query, Router, Status
from ninja.pagination import paginate

from django.db import IntegrityError, transaction
from django.http import HttpRequest
from django.utils import timezone

from apps.api.auth import current_user
from apps.api.schemas import (
    ErrorSchema,
    NominationCreateSchema,
    NominationDecisionSchema,
    NominationSchema,
    ReserveEntryFilterSchema,
    ReserveEntrySchema,
    ReserveGroupSchema,
    ReserveStatusHistorySchema,
    ReserveTransitionSchema,
)
from apps.org.models import Position
from apps.people.models import Employee
from apps.reserve.models import (
    NominationKind,
    NominationStatus,
    ReserveEntry,
    ReserveGroup,
    ReserveNomination,
)
from apps.reserve.services import ReserveError, decide_hr, decide_manager
from core.permissions import (
    apply_scope_by_path,
    can_approve_nomination,
    can_decide_reserve,
    can_manage_reserve_entry,
)

router = Router(tags=["Кадровый резерв"])


@router.get(
    "/groups",
    response=list[ReserveGroupSchema],
    summary="Группы резерва",
    description="Программы подготовки с горизонтом и предельной численностью.",
)
def list_groups(request: HttpRequest, active: bool | None = None) -> list[ReserveGroup]:
    """Возвращает группы резерва."""
    queryset = ReserveGroup.objects.all()
    if active is not None:
        queryset = queryset.filter(is_active=active)
    return list(queryset)


@router.get(
    "/entries",
    response=list[ReserveEntrySchema],
    summary="Записи резерва",
    description=(
        "Состав кадрового резерва в границах области видимости вызывающего. "
        "Сортировка по умолчанию — убывание индекса готовности: список "
        "существует ради ответа на вопрос «кто готов раньше других»."
    ),
)
@paginate
def list_entries(
    request: HttpRequest,
    filters: ReserveEntryFilterSchema = Query(...),
    ordering: str = Query(default="-readiness_index"),
):
    """Возвращает записи резерва с отбором и постраничной навигацией."""
    queryset = apply_scope_by_path(
        ReserveEntry.objects.with_related(), current_user(request), prefix="employee"
    )
    queryset = filters.filter(queryset)

    allowed = {
        "-readiness_index",
        "readiness_index",
        "inclusion_date",
        "-inclusion_date",
        "employee__last_name",
    }
    if ordering in allowed:
        queryset = queryset.order_by(ordering)

    return queryset


@router.get(
    "/entries/{uuid:public_id}",
    response={200: ReserveEntrySchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Запись резерва",
)
def entry_detail(request: HttpRequest, public_id: uuid.UUID):
    """Возвращает запись резерва с проверкой области видимости."""
    entry = ReserveEntry.objects.with_related().filter(public_id=public_id).first()
    if entry is None:
        return Status(404, {"code": "not_found", "detail": "Запись резерва не найдена"})
    user = current_user(request)
    if not can_manage_reserve_entry(user, entry) and not _is_visible(request, entry):
        return Status(403, {"code": "forbidden", "detail": "Запись вне вашей области видимости"})
    return Status(200, entry)


@router.get(
    "/entries/{uuid:public_id}/history",
    response={200: list[ReserveStatusHistorySchema], 403: ErrorSchema, 404: ErrorSchema},
    summary="История статусов записи",
    description=(
        "Смены статуса с указанием автора и основания. История неизменяема: "
        "решение о составе резерва должно оставаться прослеживаемым."
    ),
)
def entry_history(request: HttpRequest, public_id: uuid.UUID):
    """Возвращает историю смены статусов записи резерва."""
    entry = ReserveEntry.objects.filter(public_id=public_id).first()
    if entry is None:
        return Status(404, {"code": "not_found", "detail": "Запись резерва не найдена"})
    if not _is_visible(request, entry):
        return Status(403, {"code": "forbidden", "detail": "Запись вне вашей области видимости"})
    return Status(200, list(entry.status_history.select_related("changed_by")))


@router.post(
    "/entries/{uuid:public_id}/transition",
    response={200: ReserveEntrySchema, 400: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Смена статуса записи резерва",
    description=(
        "Переводит запись в новый статус согласно таблице допустимых переходов. "
        "Недопустимый переход отклоняется с пояснением: восстановить "
        "исключённого резервиста, минуя процедуру повторного выдвижения, "
        "интерфейс не позволяет."
    ),
)
def transition_entry(request: HttpRequest, public_id: uuid.UUID, payload: ReserveTransitionSchema):
    """Меняет статус записи резерва с записью в историю."""
    entry = ReserveEntry.objects.with_related().filter(public_id=public_id).first()
    if entry is None:
        return Status(404, {"code": "not_found", "detail": "Запись резерва не найдена"})
    if not can_decide_reserve(current_user(request)):
        return Status(
            403,
            {
                "code": "forbidden",
                "detail": "Решения о составе резерва принимают кадровая служба и администратор",
            },
        )

    try:
        entry.transition_to(payload.status, user=current_user(request), reason=payload.reason)
    except ValueError as exc:
        return Status(400, {"code": "invalid_transition", "detail": str(exc)})

    entry.refresh_from_db()
    return Status(200, entry)


@router.get(
    "/nominations",
    response=list[NominationSchema],
    summary="Заявки на выдвижение",
    description=(
        "Заявки в границах области видимости. Параметр `pending` оставляет "
        "только ожидающие решения — рабочий список согласующего."
    ),
)
@paginate
def list_nominations(
    request: HttpRequest,
    pending: bool = Query(default=False),
    status: str | None = Query(default=None),
):
    """Возвращает заявки на выдвижение в резерв."""
    # Специализированные методы набора запросов применяются до сужения области видимости:
    # `apply_scope_by_path` возвращает обычный набор, и объявленные в модели выборки на нём уже
    # недоступны.
    nominations = ReserveNomination.objects.with_related()
    if pending:
        nominations = nominations.pending()
    if status:
        nominations = nominations.filter(status=status)
    return apply_scope_by_path(nominations, current_user(request), prefix="employee")


@router.post(
    "/nominations",
    response={201: NominationSchema, 400: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Подача заявки на выдвижение",
    description=(
        "Создаёт заявку в состоянии «подана». Повторная заявка на ту же "
        "должность при нерассмотренной предыдущей отклоняется: два решения по "
        "одному вопросу принимал бы один и тот же согласующий."
    ),
)
def create_nomination(request: HttpRequest, payload: NominationCreateSchema):
    """Создаёт заявку на включение сотрудника в кадровый резерв."""
    employee = Employee.objects.filter(pk=payload.employee_id).first()
    if employee is None:
        return Status(404, {"code": "not_found", "detail": "Сотрудник не найден"})

    position = Position.objects.filter(pk=payload.target_position_id, is_active=True).first()
    if position is None:
        return Status(404, {"code": "not_found", "detail": "Целевая должность не найдена"})

    if not employee.can_join_reserve:
        return Status(
            400,
            {
                "code": "not_eligible",
                "detail": "Сотрудник не может быть выдвинут: проверьте статус и признак исключения",
            },
        )

    kind = payload.kind if payload.kind in NominationKind.values else NominationKind.MANAGER
    if kind == NominationKind.SELF and employee.user_id != current_user(request).pk:
        return Status(
            403,
            {
                "code": "forbidden",
                "detail": "Самовыдвижение доступно только самому сотруднику",
            },
        )

    # Создание выполняется во вложенной транзакции.
    try:
        with transaction.atomic():
            nomination = ReserveNomination.objects.create(
                employee=employee,
                target_position=position,
                reserve_group_id=payload.reserve_group_id,
                kind=kind,
                status=NominationStatus.SUBMITTED,
                nominated_by=current_user(request),
                justification=payload.justification,
                achievements=payload.achievements,
                submitted_at=timezone.now(),
            )
    except IntegrityError:
        return Status(
            400,
            {
                "code": "duplicate_nomination",
                "detail": "Заявка на эту должность уже подана и ожидает решения",
            },
        )

    return Status(201, nomination)


@router.post(
    "/nominations/{uuid:public_id}/decision",
    response={200: NominationSchema, 400: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Решение по заявке на выдвижение",
    description=(
        "Принимает решение той ступени маршрута, на которой находится заявка: "
        "согласования руководителем либо рассмотрения кадровой службой. "
        "Ступень в запросе не указывается — она определяется состоянием "
        "заявки, иначе согласующий закрывал бы обе ступени одним вызовом. "
        "Одобрение кадровой службой создаёт запись резерва в той же операции: "
        "состояния «заявка одобрена, записи нет» не существует."
    ),
)
def decide_nomination(
    request: HttpRequest, public_id: uuid.UUID, payload: NominationDecisionSchema
):
    """Проводит заявку через текущую ступень согласования."""
    nomination = ReserveNomination.objects.with_related().filter(public_id=public_id).first()
    if nomination is None:
        return Status(404, {"code": "not_found", "detail": "Заявка не найдена"})

    user = current_user(request)
    stage = nomination.status

    if stage == NominationStatus.MANAGER_REVIEW and not can_approve_nomination(user, nomination):
        return Status(
            403,
            {
                "code": "forbidden",
                "detail": "Согласующим выступает руководитель кандидата либо кадровая служба",
            },
        )
    if stage == NominationStatus.HR_REVIEW and not can_decide_reserve(user):
        return Status(403, {"code": "forbidden", "detail": "Решение принимает кадровая служба"})
    if stage not in {NominationStatus.MANAGER_REVIEW, NominationStatus.HR_REVIEW}:
        return Status(
            400,
            {
                "code": "wrong_stage",
                "detail": f"Заявка не ожидает решения: {nomination.get_status_display()}",
            },
        )

    try:
        if stage == NominationStatus.MANAGER_REVIEW:
            decide_manager(nomination, actor=user, approve=payload.approve, comment=payload.comment)
        else:
            decide_hr(
                nomination,
                actor=user,
                approve=payload.approve,
                comment=payload.comment,
                group=(
                    ReserveGroup.objects.filter(pk=payload.reserve_group_id).first()
                    if payload.reserve_group_id is not None
                    else None
                ),
            )
    except ReserveError as exc:
        return Status(400, {"code": "not_allowed", "detail": str(exc)})

    nomination.refresh_from_db()
    return Status(200, nomination)


def _is_visible(request: HttpRequest, entry: ReserveEntry) -> bool:
    """Проверяет, попадает ли запись резерва в область видимости вызывающего."""
    visible = apply_scope_by_path(
        ReserveEntry.objects.filter(pk=entry.pk), current_user(request), prefix="employee"
    )
    return visible.exists()
