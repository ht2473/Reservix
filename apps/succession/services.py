"""Преемственность: критичные должности, планы замещения, назначения."""

from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from django.db import transaction
from django.db.models import Count, Q, QuerySet
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.notifications.models import NotificationKind
from apps.notifications.services import notify_many
from apps.org.models import Position
from apps.people.models import Employee
from apps.reserve.models import ReserveEntry, ReserveStatus
from apps.reserve.services import ReserveError, change_status, rank_candidates
from apps.succession.models import (
    Appointment,
    AppointmentKind,
    CriticalityLevel,
    KeyPosition,
    PlanStatus,
    SuccessionPlan,
    SuccessionSlate,
    VacancyRisk,
)
from core.constants import ReadinessLevel, Role, Signal
from core.logging import get_logger
from core.settings_types import get_reservix_settings
from core.utils import shift_months

if TYPE_CHECKING:
    from apps.accounts.models import User

logger = get_logger("succession")

#: Смещение, применяемое при перестановке рангов в списке преемников.
RANK_OFFSET = 1000

#: Доля обеспеченности, ниже которой должность считается незакрытой.
#: Единица означает полное покрытие требуемого числа преемников.
COVERAGE_AT_RISK = 0.5


class SuccessionError(Exception):
    """Нарушение правил планирования преемственности."""


# ---------------------------------------------------------------------------
# Критичные должности
# ---------------------------------------------------------------------------
def key_positions(*, only_active: bool = True) -> QuerySet[KeyPosition]:
    """Критичные должности с посчитанной обеспеченностью."""
    # Подразделение должности выводится в перечне, а `with_related` модели его не забирает: без
    # этой связи таблица из двадцати пяти строк обходилась бы тридцатью семью запросами вместо
    # двенадцати.
    queryset = KeyPosition.objects.with_related().select_related("position__department")
    if only_active:
        queryset = queryset.filter(is_active=True)

    active_plan = Q(plans__status=PlanStatus.ACTIVE)
    counted = Q(plans__slate__removed_on__isnull=True)
    return queryset.annotate(
        successors_total=Count("plans__slate", filter=active_plan & counted, distinct=True),
        successors_ready=Count(
            "plans__slate",
            filter=active_plan & counted & Q(plans__slate__readiness_level=ReadinessLevel.HIGH),
            distinct=True,
        ),
        active_plans=Count("plans", filter=active_plan, distinct=True),
    ).order_by("-criticality", "-vacancy_risk", "position__name")


def uncovered_positions() -> QuerySet[KeyPosition]:
    """Критичные должности без действующего плана преемственности."""
    return (
        KeyPosition.objects.filter(is_active=True)
        .exclude(plans__status=PlanStatus.ACTIVE)
        .with_related()
        .order_by("-criticality", "-vacancy_risk")
    )


@transaction.atomic
def sync_key_positions(*, actor: User | None = None) -> dict[str, int]:
    """Приводит перечень критичных должностей в соответствие штатному расписанию."""
    flagged = set(
        Position.objects.filter(is_key_position=True, is_active=True).values_list("id", flat=True)
    )
    existing = dict(KeyPosition.objects.values_list("position_id", "id"))

    created = 0
    for position_id in flagged - set(existing):
        position = Position.objects.select_related("department").get(pk=position_id)
        KeyPosition.objects.create(
            position=position,
            incumbent=_current_incumbent(position),
            criticality=_default_criticality(position),
            vacancy_risk=VacancyRisk.LOW,
            required_successors=2,
            reviewed_at=timezone.localdate(),
        )
        created += 1

    # Должность, потерявшая признак критичности, не удаляется: план и список преемников по ней
    # остаются историей кадровых решений.
    retired = KeyPosition.objects.filter(is_active=True).exclude(position_id__in=flagged)
    retired_count = retired.update(is_active=False)

    # Ключи журнала не должны совпадать с полями `LogRecord`: `created` — его собственная отметка
    # времени, и попытка передать её через `extra` прерывает запись в журнал исключением, а не
    # просто теряет значение.
    logger.info(
        "Перечень критичных должностей синхронизирован",
        extra={"positions_created": created, "positions_retired": retired_count},
    )
    return {"created": created, "retired": retired_count}


def _current_incumbent(position: Position) -> Employee | None:
    """Действующий обладатель должности."""
    return (
        Employee.objects.active()
        .filter(position=position)
        .order_by("position_since", "hire_date")
        .first()
    )


def _default_criticality(position: Position) -> int:
    """Начальная критичность должности по её грейду."""
    level = position.grade.level if position.grade_id else 0
    if level >= 90:
        return CriticalityLevel.CRITICAL
    if level >= 70:
        return CriticalityLevel.HIGH
    if level >= 50:
        return CriticalityLevel.SIGNIFICANT
    return CriticalityLevel.MODERATE


# ---------------------------------------------------------------------------
# Планы преемственности
# ---------------------------------------------------------------------------
@transaction.atomic
def create_plan(
    key_position: KeyPosition,
    *,
    actor: User | None = None,
    title: str = "",
    horizon_months: int | None = None,
    activate: bool = False,
) -> SuccessionPlan:
    """Создаёт план преемственности по критичной должности."""
    if activate and key_position.plans.filter(status=PlanStatus.ACTIVE).exists():
        raise SuccessionError(
            _("По должности «%(position)s» уже действует план преемственности")
            % {"position": key_position.position.name}
        )

    options = get_reservix_settings()
    plan = SuccessionPlan.objects.create(
        key_position=key_position,
        title=title
        or str(_("Преемственность: %(position)s")) % {"position": key_position.position.name},
        status=PlanStatus.ACTIVE if activate else PlanStatus.DRAFT,
        owner=actor,
        horizon_months=horizon_months or options["SUCCESSION_HORIZON_MONTHS"],
        start_date=timezone.localdate(),
        review_date=shift_months(timezone.localdate(), 12),
        created_by=actor,
        updated_by=actor,
    )
    logger.info(
        "Создан план преемственности",
        extra={"plan_id": str(plan.public_id), "position_id": key_position.position_id},
    )
    return plan


@transaction.atomic
def activate_plan(plan: SuccessionPlan, *, actor: User | None = None) -> SuccessionPlan:
    """Вводит план в действие."""
    if plan.status == PlanStatus.ACTIVE:
        raise SuccessionError(_("План уже действует"))
    if not plan.slate.filter(removed_on__isnull=True).exists():
        raise SuccessionError(_("В плане нет ни одного преемника"))
    if plan.key_position.plans.filter(status=PlanStatus.ACTIVE).exclude(pk=plan.pk).exists():
        raise SuccessionError(_("По должности уже действует другой план"))

    plan.status = PlanStatus.ACTIVE
    plan.updated_by = actor
    plan.save(update_fields=["status", "updated_by", "updated_at"])
    return plan


@transaction.atomic
def close_plan(
    plan: SuccessionPlan, *, actor: User | None = None, reason: str = "", cancel: bool = False
) -> SuccessionPlan:
    """Закрывает либо отменяет план преемственности."""
    if plan.status in {PlanStatus.CLOSED, PlanStatus.CANCELLED}:
        raise SuccessionError(_("План уже закрыт"))

    plan.status = PlanStatus.CANCELLED if cancel else PlanStatus.CLOSED
    plan.closed_at = timezone.localdate()
    plan.notes = f"{plan.notes}\n{reason}".strip() if reason else plan.notes
    plan.updated_by = actor
    plan.save(update_fields=["status", "closed_at", "notes", "updated_by", "updated_at"])
    return plan


def plans_due_for_review() -> QuerySet[SuccessionPlan]:
    """Планы, срок пересмотра которых наступил."""
    return SuccessionPlan.objects.due_for_review().with_related().order_by("review_date")


# ---------------------------------------------------------------------------
# Список преемников
# ---------------------------------------------------------------------------
@transaction.atomic
def add_successor(
    plan: SuccessionPlan,
    employee: Employee,
    *,
    entry: ReserveEntry | None = None,
    rank: int | None = None,
    is_emergency: bool = False,
    development_focus: str = "",
) -> SuccessionSlate:
    """Включает сотрудника в список преемников плана."""
    if plan.status in {PlanStatus.CLOSED, PlanStatus.CANCELLED}:
        raise SuccessionError(_("Нельзя пополнять закрытый план"))

    linked = entry or _entry_for(employee, plan.key_position.position_id)
    level, index = _readiness_of(linked)
    next_rank = rank or _next_rank(plan)

    slate = plan.slate.filter(employee=employee).first()
    if slate is not None:
        if slate.removed_on is None:
            raise SuccessionError(
                _("%(name)s уже включён в список преемников") % {"name": employee.short_name}
            )
        slate.removed_on = None
        slate.removal_reason = ""
        slate.rank = next_rank
        slate.entry = linked
        slate.readiness_level = level
        slate.readiness_index = index
        slate.is_emergency_successor = is_emergency
        slate.development_focus = development_focus
        slate.added_on = timezone.localdate()
        slate.save()
        return slate

    return SuccessionSlate.objects.create(
        plan=plan,
        employee=employee,
        entry=linked,
        rank=next_rank,
        readiness_level=level,
        readiness_index=index,
        is_emergency_successor=is_emergency,
        development_focus=development_focus,
    )


@transaction.atomic
def remove_successor(
    slate: SuccessionSlate, *, reason: str = "", actor: User | None = None
) -> SuccessionSlate:
    """Исключает преемника из списка, сохраняя строку в истории плана."""
    if slate.removed_on is not None:
        raise SuccessionError(_("Преемник уже исключён из списка"))

    slate.removed_on = timezone.localdate()
    slate.removal_reason = reason
    slate.save(update_fields=["removed_on", "removal_reason", "updated_at"])
    compact_ranks(slate.plan)
    return slate


@transaction.atomic
def reorder_slate(plan: SuccessionPlan, order: list[int]) -> int:
    """Расставляет преемников в заданном порядке."""
    rows = {row.pk: row for row in plan.slate.filter(removed_on__isnull=True)}
    ordered = [pk for pk in order if pk in rows]
    if len(ordered) != len(rows):
        raise SuccessionError(_("Перечень преемников не совпадает с составом плана"))

    for offset, pk in enumerate(ordered, start=1):
        SuccessionSlate.objects.filter(pk=pk).update(rank=RANK_OFFSET + offset)
    for position, pk in enumerate(ordered, start=1):
        SuccessionSlate.objects.filter(pk=pk).update(rank=position)

    return len(ordered)


@transaction.atomic
def compact_ranks(plan: SuccessionPlan) -> int:
    """Убирает разрывы в нумерации после исключения преемника."""
    rows = list(plan.slate.filter(removed_on__isnull=True).order_by("rank", "pk"))
    if not rows:
        return 0
    return reorder_slate(plan, [row.pk for row in rows])


@transaction.atomic
def sync_slate_readiness(plan: SuccessionPlan) -> int:
    """Обновляет готовность преемников по связанным записям резерва."""
    updated = 0
    for slate in plan.slate.filter(removed_on__isnull=True).select_related("entry"):
        if slate.entry is None:
            continue
        level, index = _readiness_of(slate.entry)
        if slate.readiness_level != level or slate.readiness_index != index:
            slate.readiness_level = level
            slate.readiness_index = index
            slate.save(update_fields=["readiness_level", "readiness_index", "updated_at"])
            updated += 1
    return updated


def suggest_successors(
    key_position: KeyPosition, *, limit: int = 10, exclude_plan: SuccessionPlan | None = None
) -> list[dict]:
    """Предлагает кандидатов в преемники должности."""
    taken: set[int] = set()
    if exclude_plan is not None:
        taken = set(
            exclude_plan.slate.filter(removed_on__isnull=True).values_list("employee_id", flat=True)
        )

    suggestions: list[dict] = []

    for entry in (
        ReserveEntry.objects.for_position(key_position.position_id)
        .in_reserve()
        .with_related()
        .order_by("rank", "-readiness_index")
    ):
        if entry.employee_id in taken:
            continue
        suggestions.append(
            {
                "employee": entry.employee,
                "entry": entry,
                "from_reserve": True,
                "match": float(entry.readiness_index or 0),
                "readiness_level": entry.readiness_level,
            }
        )
        taken.add(entry.employee_id)

    if len(suggestions) < limit:
        pool = Employee.objects.reserve_eligible().exclude(pk__in=taken)
        for candidate in rank_candidates(
            key_position.position, pool=pool, limit=limit - len(suggestions), min_match=50
        ):
            suggestions.append(
                {
                    "employee": candidate.employee,
                    "entry": None,
                    "from_reserve": False,
                    "match": candidate.match_score,
                    "readiness_level": candidate.readiness_level,
                }
            )

    return suggestions[:limit]


def _entry_for(employee: Employee, position_id: int) -> ReserveEntry | None:
    """Действующая запись резерва сотрудника на указанную должность."""
    return (
        ReserveEntry.objects.for_employee(employee.pk)
        .for_position(position_id)
        .in_reserve()
        .first()
    )


def _readiness_of(entry: ReserveEntry | None) -> tuple[str, Decimal | None]:
    """Готовность преемника по записи резерва."""
    if entry is None:
        return ReadinessLevel.LOW, None
    return entry.readiness_level, entry.readiness_index


def _next_rank(plan: SuccessionPlan) -> int:
    """Первый свободный ранг в действующем списке преемников."""
    used = set(plan.slate.filter(removed_on__isnull=True).values_list("rank", flat=True))
    rank = 1
    while rank in used:
        rank += 1
    return rank


# ---------------------------------------------------------------------------
# Назначения
# ---------------------------------------------------------------------------
@transaction.atomic
def register_appointment(
    *,
    employee: Employee,
    position: Position,
    appointment_date: dt.date,
    kind: str = AppointmentKind.FROM_RESERVE,
    actor: User | None = None,
    plan: SuccessionPlan | None = None,
    entry: ReserveEntry | None = None,
    order_number: str = "",
    comment: str = "",
    move_employee: bool = True,
) -> Appointment:
    """Регистрирует назначение — операция, замыкающая цикл системы."""
    linked_entry = entry or _entry_for(employee, position.pk)
    months = linked_entry.months_in_reserve if linked_entry is not None else None
    index = linked_entry.readiness_index if linked_entry is not None else None
    predecessor = _incumbent_of(position, exclude=employee)

    appointment = Appointment.objects.create(
        employee=employee,
        position=position,
        plan=plan or _active_plan_for(position),
        entry=linked_entry,
        predecessor=predecessor,
        kind=kind,
        appointment_date=appointment_date,
        order_number=order_number,
        readiness_index_at_appointment=index,
        months_in_reserve=months,
        comment=comment,
        created_by=actor,
        updated_by=actor,
    )

    if linked_entry is not None and linked_entry.status in {
        ReserveStatus.CANDIDATE,
        ReserveStatus.ACTIVE,
        ReserveStatus.INACTIVE,
    }:
        try:
            change_status(
                linked_entry,
                ReserveStatus.APPOINTED,
                actor=actor,
                reason=str(_("Назначен на целевую должность")),
            )
        except ReserveError as error:
            # Недопустимый переход означает рассогласование данных, а не
            # ошибку пользователя: назначение уже состоялось в кадровом учёте.
            logger.warning(
                "Запись резерва не переведена в состояние «назначен»",
                extra={"entry_id": str(linked_entry.public_id), "error": str(error)},
            )

    removed = SuccessionSlate.objects.filter(
        employee=employee,
        removed_on__isnull=True,
        plan__key_position__position=position,
    )
    for slate in removed.select_related("plan"):
        slate.removed_on = appointment_date
        slate.removal_reason = str(_("Назначен на должность"))
        slate.save(update_fields=["removed_on", "removal_reason", "updated_at"])
        compact_ranks(slate.plan)

    if move_employee:
        employee.position = position
        employee.position_since = appointment_date
        employee.save(update_fields=["position", "position_since", "updated_at"])

    KeyPosition.objects.filter(position=position).update(incumbent=employee)

    _notify_appointment(appointment, actor=actor)
    logger.info(
        "Зарегистрировано назначение",
        extra={
            "appointment_id": str(appointment.public_id),
            "kind": kind,
            "from_reserve": linked_entry is not None,
        },
    )
    return appointment


def _incumbent_of(position: Position, *, exclude: Employee | None = None) -> Employee | None:
    """Действующий обладатель должности — предшественник назначаемого."""
    key_position = KeyPosition.objects.filter(position=position).first()
    if (
        key_position is not None
        and key_position.incumbent_id
        and (exclude is None or key_position.incumbent_id != exclude.pk)
    ):
        return key_position.incumbent

    queryset = Employee.objects.active().filter(position=position)
    if exclude is not None:
        queryset = queryset.exclude(pk=exclude.pk)
    return queryset.order_by("position_since").first()


def _active_plan_for(position: Position) -> SuccessionPlan | None:
    """Действующий план преемственности по должности."""
    return SuccessionPlan.objects.filter(
        key_position__position=position, status=PlanStatus.ACTIVE
    ).first()


def _notify_appointment(appointment: Appointment, *, actor: User | None = None) -> None:
    """Сообщает о назначении назначенному, его руководителю и кадровой службе."""
    from apps.accounts.models import User as UserModel

    recipients: list[User] = []
    if appointment.employee.user is not None:
        recipients.append(appointment.employee.user)
    manager = appointment.employee.manager
    if manager is not None and manager.user is not None:
        recipients.append(manager.user)
    recipients.extend(UserModel.objects.filter(role=Role.HR, is_active=True))

    # Стрелка в заголовке избегается сознательно.
    notify_many(
        recipients,
        NotificationKind.APPOINTMENT_MADE,
        str(_("Назначение на должность «%(position)s»: %(name)s"))
        % {
            "name": appointment.employee.short_name,
            "position": appointment.position.name,
        },
        body=str(_("Источник назначения — %(kind)s")) % {"kind": appointment.get_kind_display()},
        obj=appointment,
        actor=actor,
    )


# ---------------------------------------------------------------------------
# Показатели преемственности
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class CoverageReport:
    """Обеспеченность критичных должностей преемниками."""

    total: int = 0
    covered: int = 0
    with_ready: int = 0
    with_emergency: int = 0
    successors: int = 0
    uncovered: list[KeyPosition] = field(default_factory=list)
    by_criticality: dict[int, dict[str, int]] = field(default_factory=dict)

    @property
    def coverage(self) -> float:
        """Доля критичных должностей хотя бы с одним преемником, проценты."""
        return round(self.covered * 100 / self.total, 1) if self.total else 0.0

    @property
    def ready_coverage(self) -> float:
        """Доля должностей с преемником, готовым к назначению сейчас."""
        return round(self.with_ready * 100 / self.total, 1) if self.total else 0.0

    @property
    def bench_depth(self) -> float:
        """Среднее число преемников на критичную должность."""
        return round(self.successors / self.total, 2) if self.total else 0.0

    @property
    def signal(self) -> str:
        """Сигнальная окраска показателя покрытия."""
        if self.coverage >= 80:
            return Signal.READY
        if self.coverage >= 50:
            return Signal.DEVELOP
        return Signal.RISK


def coverage_report() -> CoverageReport:
    """Считает обеспеченность критичных должностей преемниками."""
    report = CoverageReport()

    for position in key_positions():
        report.total += 1
        successors = getattr(position, "successors_total", 0)
        ready = getattr(position, "successors_ready", 0)
        report.successors += successors

        if successors:
            report.covered += 1
        else:
            report.uncovered.append(position)
        if ready:
            report.with_ready += 1

        bucket = report.by_criticality.setdefault(
            int(position.criticality),
            {"total": 0, "covered": 0, "ready": 0, "successors": 0},
        )
        bucket["total"] += 1
        bucket["successors"] += successors
        bucket["covered"] += 1 if successors else 0
        bucket["ready"] += 1 if ready else 0

    report.with_emergency = (
        SuccessionSlate.objects.filter(
            is_emergency_successor=True,
            removed_on__isnull=True,
            plan__status=PlanStatus.ACTIVE,
        )
        .values("plan__key_position_id")
        .distinct()
        .count()
    )
    return report


@dataclass(frozen=True, slots=True)
class RiskCell:
    """Ячейка матрицы «критичность × риск освобождения»."""

    criticality: int
    risk: int
    positions: tuple[KeyPosition, ...]

    @property
    def priority(self) -> int:
        return self.criticality * self.risk

    @property
    def signal(self) -> str:
        """Окраска ячейки по приоритету планирования, диапазон 1–15."""
        if self.priority >= 9:
            return Signal.RISK
        if self.priority >= 5:
            return Signal.DEVELOP
        return Signal.READY


def risk_matrix() -> list[list[RiskCell]]:
    """Матрица критичных должностей «критичность × риск освобождения»."""
    grouped: dict[tuple[int, int], list[KeyPosition]] = {}
    for position in key_positions():
        grouped.setdefault((int(position.criticality), int(position.vacancy_risk)), []).append(
            position
        )

    matrix: list[list[RiskCell]] = []
    for risk in sorted(VacancyRisk.values, reverse=True):
        row = [
            RiskCell(
                criticality=criticality,
                risk=risk,
                positions=tuple(grouped.get((criticality, risk), [])),
            )
            for criticality in sorted(CriticalityLevel.values)
        ]
        matrix.append(row)
    return matrix


@dataclass(slots=True)
class AppointmentStatistics:
    """Результативность системы за период."""

    total: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    average_months: float | None = None
    average_index: float | None = None
    on_key_positions: int = 0

    @property
    def from_reserve(self) -> int:
        return self.by_kind.get(AppointmentKind.FROM_RESERVE, 0)

    @property
    def reserve_share(self) -> float:
        """Доля назначений из резерва, проценты."""
        return round(self.from_reserve * 100 / self.total, 1) if self.total else 0.0

    @property
    def average_years(self) -> float | None:
        """Средний срок подготовки в годах — форма для отчётности."""
        if self.average_months is None:
            return None
        return round(self.average_months / 12, 1)


def appointment_statistics(
    *, year: int | None = None, months: int | None = None
) -> AppointmentStatistics:
    """Сводка по назначениям за календарный год либо за последние месяцы."""
    queryset = Appointment.objects.all()
    if year is not None:
        queryset = queryset.in_year(year)
    elif months is not None:
        window_start = shift_months(timezone.localdate(), -months)
        queryset = queryset.filter(appointment_date__gte=window_start)

    stats = AppointmentStatistics()
    rows = list(
        queryset.values(
            "kind",
            "months_in_reserve",
            "readiness_index_at_appointment",
            "position__is_key_position",
        )
    )
    stats.total = len(rows)
    stats.by_kind = dict(Counter(row["kind"] for row in rows))
    stats.on_key_positions = sum(1 for row in rows if row["position__is_key_position"])

    durations = [row["months_in_reserve"] for row in rows if row["months_in_reserve"] is not None]
    if durations:
        stats.average_months = round(sum(durations) / len(durations), 1)

    indices = [
        float(row["readiness_index_at_appointment"])
        for row in rows
        if row["readiness_index_at_appointment"] is not None
    ]
    if indices:
        stats.average_index = round(sum(indices) / len(indices), 1)

    return stats
