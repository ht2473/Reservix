"""Кадровый резерв: пригодность, выдвижение, движение по статусам, ранжирование."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.db import transaction
from django.db.models import Count, Q, QuerySet
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.competencies.services import (
    CompetencyProfile,
    build_profile,
    build_profiles,
    resolve_competency_model,
)
from apps.notifications.models import NotificationKind
from apps.notifications.services import notify, notify_many
from apps.people.models import Employee, EmployeeStatus
from apps.reserve.models import (
    ACTIVE_RESERVE_STATUSES,
    NominationKind,
    NominationStatus,
    ReserveEntry,
    ReserveGroup,
    ReserveMembership,
    ReserveNomination,
    ReserveStatus,
)
from core.constants import ReadinessLevel, Role, Signal
from core.logging import get_logger
from core.utils import shift_months

if TYPE_CHECKING:
    from apps.accounts.models import User
    from apps.org.models import Position

logger = get_logger("reserve")

#: Срок, через который запись резерва выносится на пересмотр, месяцев.
DEFAULT_REVIEW_MONTHS = 24

#: Предельное число одновременных целевых должностей у одного резервиста.
MAX_ACTIVE_TARGETS = 3

#: Минимальный стаж в организации для выдвижения, месяцев. Значение по
#: умолчанию; группа резерва может ужесточить его собственным правилом.
MIN_TENURE_MONTHS = 6

#: Повод уведомления по завершающему состоянию записи.
STATUS_NOTIFICATION_KINDS: dict[str, str] = {
    ReserveStatus.EXCLUDED: NotificationKind.RESERVE_EXCLUDED,
    ReserveStatus.APPOINTED: NotificationKind.APPOINTMENT_MADE,
}

#: Сигнальная окраска уровня готовности для интерфейса.
READINESS_SIGNALS: dict[str, str] = {
    ReadinessLevel.HIGH: Signal.READY,
    ReadinessLevel.MEDIUM: Signal.DEVELOP,
    ReadinessLevel.LOW: Signal.RISK,
}


class ReserveError(Exception):
    """Нарушение правил работы с резервом."""


# ---------------------------------------------------------------------------
# Пригодность кандидата
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class EligibilityIssue:
    """Одно замечание к выдвижению кандидата."""

    code: str
    message: str
    is_blocking: bool = False
    signal: str = Signal.DEVELOP


@dataclass(frozen=True, slots=True)
class Eligibility:
    """Итог проверки пригодности кандидата к выдвижению на должность."""

    employee: Employee
    position: Position
    issues: tuple[EligibilityIssue, ...] = ()
    profile: CompetencyProfile | None = None

    @property
    def blockers(self) -> tuple[EligibilityIssue, ...]:
        return tuple(issue for issue in self.issues if issue.is_blocking)

    @property
    def warnings(self) -> tuple[EligibilityIssue, ...]:
        return tuple(issue for issue in self.issues if not issue.is_blocking)

    @property
    def is_eligible(self) -> bool:
        return not self.blockers

    @property
    def match_score(self) -> float:
        """Компетентностное соответствие целевой должности, проценты."""
        return self.profile.match_score if self.profile is not None else 0.0

    def raise_if_blocked(self) -> None:
        """Прерывает операцию, перечисляя препятствия."""
        blockers = self.blockers
        if blockers:
            raise ReserveError("; ".join(issue.message for issue in blockers))


def check_eligibility(
    employee: Employee,
    position: Position,
    *,
    group: ReserveGroup | None = None,
    with_profile: bool = True,
    exclude_nomination_id: int | None = None,
) -> Eligibility:
    """Проверяет, можно ли выдвинуть сотрудника на должность."""
    issues: list[EligibilityIssue] = []

    # -- Кадровое состояние -------------------------------------------------
    if employee.status != EmployeeStatus.ACTIVE:
        issues.append(
            EligibilityIssue(
                "not_active",
                str(_("Сотрудник не числится работающим: %(status)s"))
                % {"status": employee.get_status_display()},
                is_blocking=True,
                signal=Signal.RISK,
            )
        )

    if employee.is_excluded_from_reserve:
        note = employee.exclusion_note or _("основание не указано")
        issues.append(
            EligibilityIssue(
                "excluded",
                str(_("Сотрудник исключён из рассмотрения: %(note)s")) % {"note": note},
                is_blocking=True,
                signal=Signal.RISK,
            )
        )

    if employee.tenure_months < MIN_TENURE_MONTHS:
        issues.append(
            EligibilityIssue(
                "tenure",
                str(_("Стаж в организации %(actual)s месяцев при требуемых %(required)s"))
                % {"actual": employee.tenure_months, "required": MIN_TENURE_MONTHS},
                is_blocking=True,
                signal=Signal.RISK,
            )
        )

    if employee.position_id == position.pk:
        issues.append(
            EligibilityIssue(
                "same_position",
                str(_("Сотрудник уже занимает эту должность")),
                is_blocking=True,
                signal=Signal.RISK,
            )
        )

    # -- Состояние в резерве -------------------------------------------------
    active_entries = list(
        ReserveEntry.objects.for_employee(employee.pk)
        .in_reserve()
        .select_related("target_position")
    )
    if any(entry.target_position_id == position.pk for entry in active_entries):
        issues.append(
            EligibilityIssue(
                "already_in_reserve",
                str(_("Сотрудник уже состоит в резерве на эту должность")),
                is_blocking=True,
                signal=Signal.RISK,
            )
        )
    elif len(active_entries) >= MAX_ACTIVE_TARGETS:
        issues.append(
            EligibilityIssue(
                "too_many_targets",
                str(_("Сотрудник уже рассматривается на %(count)s должности"))
                % {"count": len(active_entries)},
                is_blocking=True,
                signal=Signal.RISK,
            )
        )
    elif active_entries:
        issues.append(
            EligibilityIssue(
                "other_targets",
                str(_("Сотрудник уже в резерве на другие должности: %(targets)s"))
                % {"targets": ", ".join(e.target_position.name for e in active_entries)},
                signal=Signal.NEUTRAL,
            )
        )

    competing = (
        ReserveNomination.objects.for_employee(employee.pk)
        .filter(target_position=position)
        .exclude(
            status__in=[
                NominationStatus.APPROVED,
                NominationStatus.REJECTED,
                NominationStatus.WITHDRAWN,
            ]
        )
    )
    if exclude_nomination_id is not None:
        competing = competing.exclude(pk=exclude_nomination_id)

    if competing.exists():
        issues.append(
            EligibilityIssue(
                "nomination_exists",
                str(_("По этому кандидату уже есть незакрытая заявка на должность")),
                is_blocking=True,
                signal=Signal.RISK,
            )
        )

    # -- Правила группы резерва ----------------------------------------------
    if group is not None:
        issues.extend(_group_issues(employee, group))

    # -- Компетенции ---------------------------------------------------------
    profile: CompetencyProfile | None = None
    if with_profile:
        target_model = resolve_competency_model(position_id=position.pk, grade_id=position.grade_id)
        if target_model is None:
            issues.append(
                EligibilityIssue(
                    "no_model",
                    str(_("Для целевой должности не задана модель компетенций")),
                    signal=Signal.DEVELOP,
                )
            )
        else:
            profile = build_profile(employee, target_model)
            issues.extend(_profile_issues(profile))

    return Eligibility(employee=employee, position=position, issues=tuple(issues), profile=profile)


def _group_issues(employee: Employee, group: ReserveGroup) -> list[EligibilityIssue]:
    """Замечания по правилам конкретной программы подготовки."""
    issues: list[EligibilityIssue] = []

    age = employee.age
    if group.max_age is not None and age is not None and age > group.max_age:
        issues.append(
            EligibilityIssue(
                "group_age",
                str(_("Возраст %(age)s превышает предел группы «%(group)s» — %(limit)s"))
                % {"age": age, "group": group.name, "limit": group.max_age},
                is_blocking=True,
                signal=Signal.RISK,
            )
        )

    if employee.tenure_months < group.min_tenure_months:
        issues.append(
            EligibilityIssue(
                "group_tenure",
                str(_("Стаж ниже требуемого группой «%(group)s»: %(required)s месяцев"))
                % {"group": group.name, "required": group.min_tenure_months},
                is_blocking=True,
                signal=Signal.RISK,
            )
        )

    if not group.has_free_places:
        issues.append(
            EligibilityIssue(
                "group_capacity",
                str(_("Группа «%(group)s» заполнена: %(count)s из %(capacity)s"))
                % {
                    "group": group.name,
                    "count": group.member_count,
                    "capacity": group.capacity,
                },
                is_blocking=True,
                signal=Signal.RISK,
            )
        )

    return issues


def _profile_issues(profile: CompetencyProfile) -> list[EligibilityIssue]:
    """Замечания по компетентностному соответствию."""
    issues: list[EligibilityIssue] = []

    blocking = profile.blocking_gaps
    if blocking:
        issues.append(
            EligibilityIssue(
                "critical_gap",
                str(_("Не выполнены критичные требования должности: %(items)s"))
                % {"items": ", ".join(gap.competency.name for gap in blocking[:3])},
                signal=Signal.RISK,
            )
        )

    if profile.coverage < 50:
        issues.append(
            EligibilityIssue(
                "low_coverage",
                str(_("Оценкой подтверждено лишь %(coverage)s%% требований должности"))
                % {"coverage": profile.coverage},
                signal=Signal.DEVELOP,
            )
        )

    return issues


# ---------------------------------------------------------------------------
# Выдвижение
# ---------------------------------------------------------------------------
def _is_manager_of(user: User, employee: Employee) -> bool:
    """Является ли пользователь непосредственным руководителем сотрудника."""
    manager = employee.manager
    if manager is not None and manager.user_id == user.pk:
        return True
    return bool(
        user.role == Role.MANAGER
        and user.is_department_head
        and user.department_id
        and employee.department_id == user.department_id
    )


@transaction.atomic
def submit_nomination(nomination: ReserveNomination, *, actor: User) -> ReserveNomination:
    """Подаёт заявку на рассмотрение."""
    if nomination.status != NominationStatus.DRAFT:
        raise ReserveError(_("Заявка уже подана"))

    eligibility = check_eligibility(
        nomination.employee,
        nomination.target_position,
        group=nomination.reserve_group,
        with_profile=False,
        exclude_nomination_id=nomination.pk,
    )
    eligibility.raise_if_blocked()

    skips_manager = _is_manager_of(actor, nomination.employee)
    nomination.status = (
        NominationStatus.HR_REVIEW if skips_manager else NominationStatus.MANAGER_REVIEW
    )
    nomination.submitted_at = timezone.now()
    if skips_manager:
        # Решение руководителя фиксируется явно, а не подразумевается: иначе в карточке заявки
        # остаётся пустая ступень, и читающий не понимает, согласовал руководитель кандидатуру или
        # ступень пропустили по ошибке.
        nomination.manager_reviewer = actor
        nomination.manager_decision_at = nomination.submitted_at
        nomination.manager_comment = str(_("Заявка подана непосредственным руководителем"))
    nomination.save(
        update_fields=[
            "status",
            "submitted_at",
            "manager_reviewer",
            "manager_decision_at",
            "manager_comment",
            "updated_at",
        ]
    )

    _notify_reviewers(nomination)
    logger.info(
        "Заявка на выдвижение подана",
        extra={"nomination_id": str(nomination.public_id), "status": nomination.status},
    )
    return nomination


@transaction.atomic
def decide_manager(
    nomination: ReserveNomination, *, actor: User, approve: bool, comment: str = ""
) -> ReserveNomination:
    """Решение непосредственного руководителя по заявке."""
    if nomination.status != NominationStatus.MANAGER_REVIEW:
        raise ReserveError(_("Заявка не находится на согласовании у руководителя"))

    nomination.manager_reviewer = actor
    nomination.manager_decision_at = timezone.now()
    nomination.manager_comment = comment
    nomination.status = NominationStatus.HR_REVIEW if approve else NominationStatus.REJECTED
    if not approve:
        nomination.rejection_reason = comment or str(_("Отклонено руководителем"))
    nomination.save(
        update_fields=[
            "manager_reviewer",
            "manager_decision_at",
            "manager_comment",
            "rejection_reason",
            "status",
            "updated_at",
        ]
    )

    if approve:
        _notify_reviewers(nomination)
    else:
        _notify_nomination_rejected(nomination)
    return nomination


@transaction.atomic
def decide_hr(
    nomination: ReserveNomination,
    *,
    actor: User,
    approve: bool,
    comment: str = "",
    group: ReserveGroup | None = None,
    review_months: int = DEFAULT_REVIEW_MONTHS,
) -> ReserveNomination:
    """Решение кадровой службы — последняя ступень маршрута."""
    if nomination.status != NominationStatus.HR_REVIEW:
        raise ReserveError(_("Заявка не находится на рассмотрении кадровой службы"))

    nomination.hr_reviewer = actor
    nomination.hr_decision_at = timezone.now()
    nomination.hr_comment = comment

    if not approve:
        nomination.status = NominationStatus.REJECTED
        nomination.rejection_reason = comment or str(_("Отклонено кадровой службой"))
        nomination.save(
            update_fields=[
                "hr_reviewer",
                "hr_decision_at",
                "hr_comment",
                "rejection_reason",
                "status",
                "updated_at",
            ]
        )
        _notify_nomination_rejected(nomination)
        return nomination

    entry = create_entry(
        employee=nomination.employee,
        position=nomination.target_position,
        group=group or nomination.reserve_group,
        actor=actor,
        justification=nomination.justification,
        review_months=review_months,
    )

    nomination.status = NominationStatus.APPROVED
    nomination.entry = entry
    nomination.save(
        update_fields=[
            "hr_reviewer",
            "hr_decision_at",
            "hr_comment",
            "status",
            "entry",
            "updated_at",
        ]
    )

    initiator = nomination.nominated_by
    if initiator is not None and initiator.pk != actor.pk:
        notify(
            initiator,
            NotificationKind.NOMINATION_APPROVED,
            str(_("Заявка одобрена: %(name)s")) % {"name": nomination.employee.short_name},
            body=str(_("Кандидат включён в резерв на должность «%(position)s»"))
            % {"position": nomination.target_position.name},
            obj=nomination,
            actor=actor,
        )

    logger.info(
        "Заявка одобрена, создана запись резерва",
        extra={
            "nomination_id": str(nomination.public_id),
            "entry_id": str(entry.public_id),
        },
    )
    return nomination


@transaction.atomic
def withdraw_nomination(
    nomination: ReserveNomination, *, actor: User, reason: str = ""
) -> ReserveNomination:
    """Отзывает поданную заявку — действие инициатора либо кадровой службы."""
    if nomination.status in {
        NominationStatus.APPROVED,
        NominationStatus.REJECTED,
        NominationStatus.WITHDRAWN,
    }:
        raise ReserveError(_("Решение по заявке уже принято"))

    nomination.status = NominationStatus.WITHDRAWN
    nomination.rejection_reason = reason or str(_("Отозвана инициатором"))
    nomination.save(update_fields=["status", "rejection_reason", "updated_at"])
    logger.info(
        "Заявка отозвана",
        extra={"nomination_id": str(nomination.public_id), "actor_id": actor.pk},
    )
    return nomination


def _reviewer_users(nomination: ReserveNomination) -> list[User]:
    """Кому адресована заявка на текущей ступени."""
    from apps.accounts.models import User as UserModel

    if nomination.status == NominationStatus.MANAGER_REVIEW:
        manager = nomination.employee.manager
        if manager is not None and manager.user_id:
            return list(UserModel.objects.filter(pk=manager.user_id, is_active=True))
        return list(
            UserModel.objects.filter(
                role=Role.MANAGER,
                is_department_head=True,
                department_id=nomination.employee.department_id,
                is_active=True,
            )
        )

    if nomination.status == NominationStatus.HR_REVIEW:
        return list(UserModel.objects.filter(role=Role.HR, is_active=True))

    return []


def _notify_reviewers(nomination: ReserveNomination) -> None:
    """Уведомляет согласующих текущей ступени."""
    recipients = _reviewer_users(nomination)
    if not recipients:
        logger.warning(
            "Для заявки не найден согласующий",
            extra={"nomination_id": str(nomination.public_id), "status": nomination.status},
        )
        return

    notify_many(
        recipients,
        NotificationKind.NOMINATION_SUBMITTED,
        str(_("Заявка на включение в резерв: %(name)s")) % {"name": nomination.employee.short_name},
        body=str(_("Целевая должность — «%(position)s»"))
        % {"position": nomination.target_position.name},
        obj=nomination,
        actor=nomination.nominated_by,
    )


def _notify_nomination_rejected(nomination: ReserveNomination) -> None:
    """Сообщает инициатору и кандидату об отказе."""
    recipients: list[User] = []
    if nomination.nominated_by is not None:
        recipients.append(nomination.nominated_by)
    candidate_user = nomination.employee.user
    if candidate_user is not None and nomination.kind == NominationKind.SELF:
        recipients.append(candidate_user)

    if not recipients:
        return

    notify_many(
        recipients,
        NotificationKind.NOMINATION_REJECTED,
        str(_("Заявка отклонена: %(name)s")) % {"name": nomination.employee.short_name},
        body=nomination.rejection_reason,
        obj=nomination,
    )


# ---------------------------------------------------------------------------
# Записи резерва
# ---------------------------------------------------------------------------
@transaction.atomic
def create_entry(
    *,
    employee: Employee,
    position: Position,
    group: ReserveGroup | None = None,
    actor: User | None = None,
    justification: str = "",
    review_months: int = DEFAULT_REVIEW_MONTHS,
    status: str = ReserveStatus.CANDIDATE,
) -> ReserveEntry:
    """Создаёт запись резерва и сопутствующие ей объекты."""
    existing = (
        ReserveEntry.objects.in_reserve()
        .filter(employee=employee, target_position=position)
        .first()
    )
    if existing is not None:
        raise ReserveError(
            _("Сотрудник уже состоит в резерве на должность «%(position)s»")
            % {"position": position.name}
        )

    today = timezone.localdate()
    entry = ReserveEntry.objects.create(
        employee=employee,
        target_position=position,
        reserve_group=group,
        status=status,
        inclusion_date=today,
        review_date=shift_months(today, review_months),
        justification=justification,
        created_by=actor,
        updated_by=actor,
    )

    entry.status_history.create(
        old_status="",
        new_status=status,
        changed_by=actor,
        reason=str(_("Включение в кадровый резерв")),
    )

    if group is not None:
        ReserveMembership.objects.get_or_create(
            group=group,
            employee=employee,
            left_at=None,
            defaults={"entry": entry, "joined_at": today},
        )

    refresh_readiness(entry)
    rerank_position(position.pk)

    if employee.user is not None:
        notify(
            employee.user,
            NotificationKind.RESERVE_INCLUDED,
            str(_("Вы включены в кадровый резерв")),
            body=str(_("Целевая должность — «%(position)s»")) % {"position": position.name},
            obj=entry,
            actor=actor,
        )

    return entry


@transaction.atomic
def change_status(
    entry: ReserveEntry, new_status: str, *, actor: User | None = None, reason: str = ""
) -> ReserveEntry:
    """Переводит запись резерва в новое состояние."""
    try:
        entry.transition_to(new_status, user=actor, reason=reason)
    except ValueError as error:
        raise ReserveError(str(error)) from error

    if new_status in {ReserveStatus.EXCLUDED, ReserveStatus.APPOINTED}:
        entry.memberships.filter(left_at__isnull=True).update(
            left_at=timezone.localdate(),
            leave_reason=reason or entry.get_status_display(),
        )
        # Ранг закрытой записи не удерживает место: кандидаты, стоявшие ниже исключённого,
        # поднимаются в списке преемников сразу, а не после ближайшего пересчёта индексов.
        ReserveEntry.objects.filter(pk=entry.pk).update(rank=None)

    rerank_position(entry.target_position_id)

    if entry.employee.user is not None:
        kind = STATUS_NOTIFICATION_KINDS.get(new_status, NotificationKind.RESERVE_STATUS)
        notify(
            entry.employee.user,
            kind,
            str(_("Статус в резерве изменён: %(status)s")) % {"status": entry.get_status_display()},
            body=reason,
            obj=entry,
            actor=actor,
        )

    logger.info(
        "Статус записи резерва изменён",
        extra={"entry_id": str(entry.public_id), "status": new_status},
    )
    return entry


@transaction.atomic
def schedule_review(
    entry: ReserveEntry, *, months: int = DEFAULT_REVIEW_MONTHS, actor: User | None = None
) -> ReserveEntry:
    """Переносит дату очередного пересмотра записи от сегодняшнего дня."""
    entry.review_date = shift_months(timezone.localdate(), months)
    entry.updated_by = actor
    entry.save(update_fields=["review_date", "updated_by", "updated_at"])
    return entry


def entries_due_for_review(user: User | None = None) -> QuerySet[ReserveEntry]:
    """Записи, срок пересмотра которых наступил, в порядке давности."""
    queryset = ReserveEntry.objects.due_for_review().with_related().order_by("review_date")
    if user is None:
        return queryset

    from core.permissions import apply_scope_by_path

    return apply_scope_by_path(queryset, user, prefix="employee")


# ---------------------------------------------------------------------------
# Готовность и ранжирование
# ---------------------------------------------------------------------------
def readiness_score(entry: ReserveEntry) -> float:
    """Композитный индекс готовности резервиста, 0–100."""
    from apps.analytics.services import AnalyticsError, readiness_index

    try:
        return readiness_index(entry)
    except AnalyticsError:
        # Схема оценивания не настроена или непригодна.
        logger.warning("Индекс готовности не рассчитан: схема оценивания непригодна")
        return float(entry.readiness_index or 0)


def target_profile(entry: ReserveEntry) -> CompetencyProfile | None:
    """Профиль соответствия резервиста требованиям его целевой должности."""
    position = entry.target_position
    model = resolve_competency_model(position_id=position.pk, grade_id=position.grade_id)
    if model is None:
        return None
    return build_profile(entry.employee, model)


def refresh_readiness(entry: ReserveEntry, *, actor: User | None = None) -> ReserveEntry:
    """Пересчитывает индекс готовности записи и уровень, выводимый из него."""
    previous_level = entry.readiness_level
    entry.apply_readiness(readiness_score(entry))

    if entry.readiness_level != previous_level and entry.employee.user is not None:
        notify(
            entry.employee.user,
            NotificationKind.READINESS_CHANGED,
            str(_("Уровень готовности изменён: %(level)s"))
            % {"level": entry.get_readiness_level_display()},
            obj=entry,
            actor=actor,
        )
    return entry


def refresh_readiness_bulk(entries: QuerySet[ReserveEntry] | None = None) -> int:
    """Пересчитывает индексы для набора записей одной операцией."""
    from apps.analytics.services import AnalyticsError, recalculate

    try:
        result = recalculate(entries)
    except AnalyticsError as error:
        logger.warning("Пересчёт индексов не выполнен: %s", error)
        return 0

    logger.info(
        "Пересчитаны индексы готовности",
        extra={
            "entries": result.entries_updated,
            "snapshots": result.snapshots_created,
            "levels": result.level_changes,
        },
    )
    return result.entries_updated


@transaction.atomic
def rerank_position(position_id: int) -> int:
    """Расставляет ранги кандидатов на должность по индексу готовности."""
    entries = list(
        ReserveEntry.objects.filter(target_position_id=position_id)
        .in_reserve()
        .order_by("-readiness_index", "inclusion_date", "pk")
    )

    ordered = sorted(
        entries,
        key=lambda entry: (
            entry.readiness_index is None,
            -float(entry.readiness_index or 0),
            entry.inclusion_date,
            entry.pk,
        ),
    )

    changed = 0
    for position, entry in enumerate(ordered, start=1):
        if entry.rank != position:
            ReserveEntry.objects.filter(pk=entry.pk).update(rank=position)
            changed += 1
    return changed


@dataclass(frozen=True, slots=True)
class Candidate:
    """Кандидат на должность, ещё не состоящий в резерве на неё."""

    employee: Employee
    profile: CompetencyProfile
    eligibility: Eligibility | None = None

    @property
    def match_score(self) -> float:
        return self.profile.match_score

    @property
    def coverage(self) -> float:
        return self.profile.coverage

    @property
    def is_blocked(self) -> bool:
        return self.profile.is_blocked

    @property
    def readiness_level(self) -> str:
        from core.settings_types import get_reservix_settings

        options = get_reservix_settings()
        return ReadinessLevel.from_index(
            self.match_score,
            (options["READINESS_THRESHOLD_HIGH"], options["READINESS_THRESHOLD_MEDIUM"]),
        )


def rank_candidates(
    position: Position,
    *,
    pool: QuerySet[Employee] | None = None,
    limit: int = 20,
    min_match: float = 0.0,
) -> list[Candidate]:
    """Подбирает сотрудников, подходящих на должность, в порядке соответствия."""
    model = resolve_competency_model(position_id=position.pk, grade_id=position.grade_id)
    if model is None:
        return []

    candidates_qs = pool if pool is not None else Employee.objects.reserve_eligible()
    already = set(
        ReserveEntry.objects.for_position(position.pk)
        .in_reserve()
        .values_list("employee_id", flat=True)
    )
    employees = [
        employee
        for employee in candidates_qs.exclude(position_id=position.pk).select_related(
            "department", "branch", "position"
        )
        if employee.pk not in already
    ]
    if not employees:
        return []

    profiles = build_profiles(employees, model)
    result = [
        Candidate(employee=employee, profile=profiles[employee.pk])
        for employee in employees
        if employee.pk in profiles and profiles[employee.pk].match_score >= min_match
    ]
    result.sort(key=lambda item: (-item.match_score, -item.coverage, item.employee.last_name))
    return result[:limit]


# ---------------------------------------------------------------------------
# Сводные показатели резерва
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ReserveSummary:
    """Состояние резерва в цифрах — шапка страницы кадровой службы."""

    total: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    by_readiness: dict[str, int] = field(default_factory=dict)
    positions_covered: int = 0
    overdue_reviews: int = 0
    pending_nominations: int = 0
    appointed_last_year: int = 0

    @property
    def active(self) -> int:
        return sum(self.by_status.get(status, 0) for status in ACTIVE_RESERVE_STATUSES)

    @property
    def ready_now(self) -> int:
        return self.by_readiness.get(ReadinessLevel.HIGH, 0)

    @property
    def ready_share(self) -> float:
        """Доля готовых к назначению среди состоящих в резерве, проценты."""
        active = self.active
        return round(self.ready_now * 100 / active, 1) if active else 0.0


def reserve_summary(*, user: User | None = None) -> ReserveSummary:
    """Собирает сводку по резерву в границах видимости пользователя."""
    from core.permissions import apply_scope_by_path

    entries = ReserveEntry.objects.all()
    nominations = ReserveNomination.objects.pending()

    if user is not None:
        entries = apply_scope_by_path(entries, user, prefix="employee")
        nominations = apply_scope_by_path(nominations, user, prefix="employee")

    summary = ReserveSummary()

    for status_row in entries.values("status").annotate(total=Count("id")):
        summary.by_status[status_row["status"]] = status_row["total"]
    summary.total = sum(summary.by_status.values())

    for level_row in (
        entries.filter(status__in=ACTIVE_RESERVE_STATUSES)
        .values("readiness_level")
        .annotate(total=Count("id"))
    ):
        summary.by_readiness[level_row["readiness_level"]] = level_row["total"]

    summary.positions_covered = (
        entries.filter(status__in=ACTIVE_RESERVE_STATUSES)
        .values("target_position_id")
        .distinct()
        .count()
    )
    summary.overdue_reviews = entries.filter(
        status__in=ACTIVE_RESERVE_STATUSES, review_date__lt=timezone.localdate()
    ).count()
    summary.pending_nominations = nominations.count()
    summary.appointed_last_year = entries.filter(
        status=ReserveStatus.APPOINTED,
        exclusion_date__gte=shift_months(timezone.localdate(), -12),
    ).count()

    return summary


#: Роли, которым доступен раздел кадрового резерва.
RESERVE_SECTION_ROLES: frozenset[str] = frozenset({Role.ADMIN, Role.HR, Role.MANAGER})


def can_open_entry(user: User, entry: ReserveEntry | None) -> bool:
    """Открывается ли карточка записи резерва этому пользователю."""
    if entry is None:
        return False

    from core.permissions import can_view_employee

    return user.role in RESERVE_SECTION_ROLES and can_view_employee(user, entry.employee)


def position_bench(position_id: int, viewer: User | None = None) -> list[ReserveEntry]:
    """Скамейка кандидатов на должность в порядке ранга."""
    entries = list(
        ReserveEntry.objects.for_position(position_id)
        .in_reserve()
        .with_related()
        .order_by("rank", "-readiness_index")
    )

    for entry in entries:
        entry.can_open = viewer is not None and can_open_entry(viewer, entry)

    return entries


def pending_for_user(user: User) -> QuerySet[ReserveNomination]:
    """Заявки, ожидающие решения текущего пользователя."""
    queryset = ReserveNomination.objects.with_related()

    if user.role in {Role.ADMIN, Role.HR}:
        from core.permissions import apply_scope_by_path

        return apply_scope_by_path(queryset.awaiting_hr(), user, prefix="employee").order_by(
            "submitted_at"
        )

    if user.role == Role.MANAGER:
        condition = Q(employee__manager__user_id=user.pk)
        if user.is_department_head and user.department_id:
            condition |= Q(employee__department_id=user.department_id)
        # Собственную заявку руководитель не согласует.
        return (
            queryset.awaiting_manager()
            .filter(condition)
            .exclude(employee__user_id=user.pk)
            .order_by("submitted_at")
        )

    return queryset.none()


def reserve_funnel(*, user: User | None = None) -> list[dict[str, object]]:
    """Численность резерва по стадиям процесса: кандидат — резервист — готов — назначен."""
    from core.permissions import apply_scope_by_path

    entries = ReserveEntry.objects.all()
    if user is not None:
        entries = apply_scope_by_path(entries, user, prefix="employee")

    counts = entries.aggregate(
        candidates=Count("id", filter=Q(status=ReserveStatus.CANDIDATE)),
        active=Count("id", filter=Q(status=ReserveStatus.ACTIVE)),
        ready=Count(
            "id",
            filter=Q(status=ReserveStatus.ACTIVE, readiness_level=ReadinessLevel.HIGH),
        ),
        appointed=Count("id", filter=Q(status=ReserveStatus.APPOINTED)),
    )

    return [
        {"stage": "candidate", "label": str(_("Кандидаты")), "count": counts["candidates"]},
        {"stage": "active", "label": str(_("В резерве")), "count": counts["active"]},
        {"stage": "ready", "label": str(_("Готовы к назначению")), "count": counts["ready"]},
        {"stage": "appointed", "label": str(_("Назначены")), "count": counts["appointed"]},
    ]


def readiness_signal(level: str) -> str:
    """Сигнальная окраска уровня готовности для интерфейса."""
    return READINESS_SIGNALS.get(level, Signal.NEUTRAL)
