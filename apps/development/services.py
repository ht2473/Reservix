"""Развитие резервистов: индивидуальные планы, мероприятия, наставничество."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.db.models import Avg, Count, Q, QuerySet
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.development.models import (
    ActivityEvidence,
    ActivityStatus,
    ActivityType,
    DevelopmentActivity,
    DevelopmentPlan,
    EvidenceKind,
    LearningResource,
    Mentorship,
    MentorshipStatus,
    PlanStatus,
)
from apps.notifications.models import NotificationKind
from apps.notifications.services import notify
from core.constants import PRIVILEGED_ROLES, Role, Signal
from core.logging import get_logger
from core.settings_types import get_reservix_settings

if TYPE_CHECKING:
    from apps.accounts.models import User
    from apps.competencies.services import CompetencyProfile, RequirementGap
    from apps.people.models import Employee
    from apps.reserve.models import ReserveEntry

logger = get_logger("development")

#: Горизонт плана развития по умолчанию, месяцев.
DEFAULT_HORIZON_MONTHS = 12

#: Предельное число мероприятий, предлагаемых при построении плана по разрывам.
MAX_SUGGESTED_ACTIVITIES = 6

#: Вклад мероприятия в выполнение плана по видам развития.
ACTIVITY_WEIGHTS: dict[str, int] = {
    ActivityType.ROTATION: 5,
    ActivityType.INTERNSHIP: 5,
    ActivityType.PROJECT: 4,
    ActivityType.TASK: 3,
    ActivityType.MENTORING: 3,
    ActivityType.TRAINING: 2,
    ActivityType.COURSE: 2,
    ActivityType.CONFERENCE: 1,
    ActivityType.SELF_STUDY: 1,
}


class DevelopmentError(Exception):
    """Нарушение правил ведения планов развития."""


# ---------------------------------------------------------------------------
# Сводки
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class DevelopmentSummary:
    """Состояние развития в цифрах — шапка страницы раздела."""

    plans: int = 0
    active_plans: int = 0
    on_approval: int = 0
    completed_plans: int = 0
    average_completion: float = 0.0
    activities: int = 0
    overdue_activities: int = 0
    due_soon: int = 0
    unconfirmed: int = 0
    mentorships: int = 0
    meetings_due: int = 0

    @property
    def signal(self) -> str:
        """Общая оценка состояния развития."""
        if not self.activities:
            return Signal.NEUTRAL
        overdue_share = self.overdue_activities * 100 / self.activities
        if overdue_share >= 25:
            return Signal.RISK
        if overdue_share >= 10:
            return Signal.DEVELOP
        return Signal.READY


def development_summary(*, user: User | None = None) -> DevelopmentSummary:
    """Собирает сводку по развитию в границах видимости пользователя."""
    plans = DevelopmentPlan.objects.all()
    activities = DevelopmentActivity.objects.all()
    pairs = Mentorship.objects.all()

    if user is not None:
        from core.permissions import apply_scope_by_path

        plans = apply_scope_by_path(plans, user, prefix="employee")
        activities = apply_scope_by_path(activities, user, prefix="plan__employee")
        pairs = apply_scope_by_path(pairs, user, prefix="mentee")

    today = timezone.localdate()
    horizon = today + dt.timedelta(days=get_reservix_settings()["ACTIVITY_DUE_SOON_DAYS"])
    open_states = [ActivityStatus.PENDING, ActivityStatus.IN_PROGRESS]

    plan_stats = plans.aggregate(
        total=Count("id"),
        active=Count("id", filter=Q(status=PlanStatus.ACTIVE)),
        on_approval=Count("id", filter=Q(status=PlanStatus.ON_APPROVAL)),
        completed=Count("id", filter=Q(status=PlanStatus.COMPLETED)),
        average=Avg("completion_rate", filter=Q(status=PlanStatus.ACTIVE)),
    )

    activity_stats = activities.aggregate(
        total=Count("id"),
        overdue=Count("id", filter=Q(due_date__lt=today, status__in=open_states)),
        due_soon=Count(
            "id", filter=Q(due_date__gte=today, due_date__lte=horizon, status__in=open_states)
        ),
        unconfirmed=Count("id", filter=Q(status=ActivityStatus.COMPLETED, evidences__isnull=True)),
    )

    return DevelopmentSummary(
        plans=plan_stats["total"] or 0,
        active_plans=plan_stats["active"] or 0,
        on_approval=plan_stats["on_approval"] or 0,
        completed_plans=plan_stats["completed"] or 0,
        average_completion=round(plan_stats["average"] or 0, 1),
        activities=activity_stats["total"] or 0,
        overdue_activities=activity_stats["overdue"] or 0,
        due_soon=activity_stats["due_soon"] or 0,
        unconfirmed=activity_stats["unconfirmed"] or 0,
        mentorships=pairs.filter(status=MentorshipStatus.ACTIVE).count(),
        meetings_due=_meetings_due_count(pairs),
    )


def _meetings_due_count(pairs: QuerySet[Mentorship]) -> int:
    """Число пар, у которых наступил срок очередной встречи."""
    today = timezone.localdate()
    rows = pairs.filter(status=MentorshipStatus.ACTIVE).values_list(
        "last_meeting_at", "meeting_frequency_days"
    )
    return sum(
        1
        for last_meeting, frequency in rows
        if last_meeting is None or last_meeting + dt.timedelta(days=frequency) <= today
    )


# ---------------------------------------------------------------------------
# Планы развития
# ---------------------------------------------------------------------------
def plans_for(user: User) -> QuerySet[DevelopmentPlan]:
    """Планы развития, доступные пользователю."""
    from core.permissions import apply_scope_by_path, scope_condition

    queryset = DevelopmentPlan.objects.with_related()
    if user.role in PRIVILEGED_ROLES:
        return apply_scope_by_path(queryset, user, prefix="employee")

    condition = scope_condition(user, prefix="employee")

    if user.role == Role.MENTOR:
        condition |= Q(employee__mentorships__mentor=user) & Q(
            employee__mentorships__status=MentorshipStatus.ACTIVE
        )

    return queryset.filter(condition).distinct()


@transaction.atomic
def create_plan(
    employee: Employee,
    *,
    actor: User,
    title: str = "",
    goal: str = "",
    entry: ReserveEntry | None = None,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    supervisor: User | None = None,
) -> DevelopmentPlan:
    """Создаёт план развития сотрудника."""
    from core.utils import shift_months

    existing = DevelopmentPlan.objects.for_employee(employee.pk).open().first()
    if existing is not None:
        raise DevelopmentError(
            _("У сотрудника уже есть незакрытый план развития: %(title)s")
            % {"title": existing.title}
        )

    start = start_date or timezone.localdate()
    end = end_date or shift_months(start, DEFAULT_HORIZON_MONTHS)
    if end < start:
        raise DevelopmentError(_("Дата окончания плана раньше даты начала"))

    if not title:
        target = entry.target_position.name if entry is not None else employee.position_title
        title = f"План развития: {target}" if target else "Индивидуальный план развития"

    if not goal:
        goal = _default_goal(employee, entry)

    plan = DevelopmentPlan.objects.create(
        employee=employee,
        entry=entry,
        title=title[:255],
        goal=goal,
        status=PlanStatus.DRAFT,
        start_date=start,
        end_date=end,
        supervisor=supervisor or _supervisor_of(employee),
        created_by=actor,
        updated_by=actor,
    )

    logger.info(
        "Создан план развития",
        extra={"plan_id": str(plan.public_id), "employee_id": employee.pk},
    )
    return plan


def _default_goal(employee: Employee, entry: ReserveEntry | None) -> str:
    """Формулирует цель плана из целевой должности либо текущей роли."""
    if entry is not None:
        return (
            f"Достижение готовности к назначению на должность "
            f"«{entry.target_position.name}» в установленный срок."
        )
    return (
        f"Развитие компетенций, требуемых должностью "
        f"«{employee.position_title or employee.position}»."
    )


def _supervisor_of(employee: Employee) -> User | None:
    """Пользователь-руководитель сотрудника, если у того есть учётная запись."""
    manager = employee.manager
    if manager is None:
        return None
    return manager.user


@dataclass(frozen=True, slots=True)
class ActivitySuggestion:
    """Предложение мероприятия, закрывающего конкретный разрыв."""

    competency_id: int
    competency_name: str
    target_level: int
    actual_level: float | None
    lost_points: float
    activity_type: str
    title: str
    resource: LearningResource | None = None

    @property
    def gap(self) -> float:
        return round(self.target_level - (self.actual_level or 0), 2)


def suggest_activities(
    profile: CompetencyProfile,
    *,
    limit: int = MAX_SUGGESTED_ACTIVITIES,
) -> list[ActivitySuggestion]:
    """Подбирает мероприятия под разрывы профиля компетенций."""
    suggestions: list[ActivitySuggestion] = []

    priorities = [gap for gap in profile.gaps if not gap.is_satisfied]
    priorities.sort(key=lambda gap: -gap.gap * float(gap.weight))

    for gap in priorities[:limit]:
        activity_type = _activity_type_for(gap)
        resource = _resource_for(gap)
        suggestions.append(
            ActivitySuggestion(
                competency_id=gap.competency.pk,
                competency_name=gap.competency.name,
                target_level=gap.target,
                actual_level=gap.actual,
                lost_points=round(gap.gap * float(gap.weight), 3),
                activity_type=activity_type,
                title=_suggestion_title(gap, activity_type, resource),
                resource=resource,
            )
        )
    return suggestions


def _activity_type_for(gap: RequirementGap) -> str:
    """Подбирает вид развития по величине разрыва."""
    if gap.gap >= 2:
        return ActivityType.TRAINING
    if gap.gap >= 1:
        return ActivityType.MENTORING
    return ActivityType.TASK


def _resource_for(gap: RequirementGap) -> LearningResource | None:
    """Подбирает учебный ресурс, развивающий компетенцию до целевого уровня."""
    return (
        LearningResource.objects.filter(
            is_active=True,
            competencies=gap.competency,
            target_level_min__lte=gap.target,
            target_level_max__gte=gap.target,
        )
        .order_by("-rating", "-is_internal")
        .first()
    )


def _suggestion_title(gap: RequirementGap, activity_type: str, resource) -> str:
    """Формулирует наименование предлагаемого мероприятия."""
    if resource is not None:
        return resource.name[:255]
    verbs: dict[str, str] = {
        ActivityType.TRAINING: "Обучение",
        ActivityType.MENTORING: "Работа с наставником",
        ActivityType.TASK: "Практическая задача",
    }
    verb = verbs.get(activity_type, "Развитие")
    return f"{verb}: {gap.competency.name}"[:255]


@transaction.atomic
def fill_plan_from_gaps(plan: DevelopmentPlan, *, actor: User, limit: int = 0) -> int:
    """Наполняет план мероприятиями по разрывам профиля компетенций."""
    from apps.competencies.services import build_profile

    if plan.status not in {PlanStatus.DRAFT, PlanStatus.ON_APPROVAL}:
        raise DevelopmentError(_("Наполнять можно только черновик плана"))

    competency_model = None
    entry = plan.entry
    if entry is not None:
        from apps.reserve.services import target_profile

        target = target_profile(entry)
        if target is not None:
            competency_model = target.competency_model

    profile = build_profile(plan.employee, competency_model)
    suggestions = suggest_activities(profile, limit=limit or MAX_SUGGESTED_ACTIVITIES)
    if not suggestions:
        return 0

    existing = set(plan.activities.values_list("competency_id", flat=True))
    step = max((plan.end_date - plan.start_date).days // (len(suggestions) + 1), 14)

    created = 0
    for index, suggestion in enumerate(suggestions, start=1):
        if suggestion.competency_id in existing:
            continue
        due = min(plan.start_date + dt.timedelta(days=step * index), plan.end_date)
        DevelopmentActivity.objects.create(
            plan=plan,
            title=suggestion.title,
            activity_type=suggestion.activity_type,
            description=(
                f"Закрывает разрыв по компетенции «{suggestion.competency_name}»: "
                f"текущий уровень {suggestion.actual_level or 0}, целевой "
                f"{suggestion.target_level}."
            ),
            competency_id=suggestion.competency_id,
            resource=suggestion.resource,
            status=ActivityStatus.PENDING,
            start_date=plan.start_date,
            due_date=due,
            # Ответственный — учётная запись сотрудника: поле ссылается на пользователя, а не на
            # кадровую карточку.
            responsible=plan.employee.user,
            weight=ACTIVITY_WEIGHTS.get(suggestion.activity_type, 1),
            planned_hours=suggestion.resource.duration_hours if suggestion.resource else 0,
        )
        created += 1

    plan.recalculate_completion()
    # Ключ `created` в дополнительных полях записи журнала недопустим: он занят самим объектом
    # записи (время создания), и его подмена приводит к отказу при формировании записи, а не к
    # искажённому выводу.
    logger.info(
        "План наполнен мероприятиями по разрывам",
        extra={"plan_id": str(plan.public_id), "activities_created": created},
    )
    return created


def submit_plan(plan: DevelopmentPlan, *, actor: User) -> DevelopmentPlan:
    """Отправляет план на утверждение руководителю."""
    if plan.status != PlanStatus.DRAFT:
        raise DevelopmentError(_("На утверждение отправляется только черновик"))
    if not plan.activities.exists():
        raise DevelopmentError(_("В плане нет мероприятий: утверждать нечего"))

    plan.status = PlanStatus.ON_APPROVAL
    plan.updated_by = actor
    plan.save(update_fields=["status", "updated_by", "updated_at"])

    if plan.supervisor is not None:
        notify(
            plan.supervisor,
            NotificationKind.PLAN_ASSIGNED,
            title=str(
                _("План развития на утверждение: %(name)s") % {"name": plan.employee.short_name}
            ),
            body=plan.title,
            obj=plan,
            actor=actor,
        )
    return plan


@transaction.atomic
def approve_plan(plan: DevelopmentPlan, *, actor: User) -> DevelopmentPlan:
    """Утверждает план и вводит его в действие."""
    if plan.status not in {PlanStatus.ON_APPROVAL, PlanStatus.DRAFT}:
        raise DevelopmentError(_("Утвердить можно только план, ожидающий согласования"))

    plan.status = PlanStatus.ACTIVE
    plan.approved_by = actor
    plan.approved_at = timezone.now()
    plan.updated_by = actor
    plan.save(update_fields=["status", "approved_by", "approved_at", "updated_by", "updated_at"])

    if plan.employee.user is not None:
        notify(
            plan.employee.user,
            NotificationKind.PLAN_ASSIGNED,
            title=str(_("Утверждён индивидуальный план развития")),
            body=str(
                _("%(plan)s. Срок: до %(date)s.")
                % {"plan": plan.title, "date": f"{plan.end_date:%d.%m.%Y}"}
            ),
            obj=plan,
            actor=actor,
        )

    logger.info("План развития утверждён", extra={"plan_id": str(plan.public_id)})
    return plan


@transaction.atomic
def complete_plan(plan: DevelopmentPlan, *, actor: User, summary: str = "") -> DevelopmentPlan:
    """Завершает план с подведением итога."""
    if plan.status != PlanStatus.ACTIVE:
        raise DevelopmentError(_("Завершить можно только действующий план"))
    if not summary.strip():
        raise DevelopmentError(_("Укажите итог выполнения плана"))

    plan.recalculate_completion()
    plan.status = PlanStatus.COMPLETED
    plan.summary = summary.strip()
    plan.updated_by = actor
    plan.save(update_fields=["status", "summary", "updated_by", "updated_at"])

    # Завершение плана — повод пересчитать готовность: закрытые разрывы
    # меняют компетентностное соответствие, а значит и индекс.
    entry = plan.entry
    if entry is not None:
        from apps.reserve.services import refresh_readiness

        refresh_readiness(entry, actor=actor)

    return plan


def cancel_plan(plan: DevelopmentPlan, *, actor: User, reason: str) -> DevelopmentPlan:
    """Отменяет план с указанием основания."""
    if plan.status in {PlanStatus.COMPLETED, PlanStatus.CANCELLED}:
        raise DevelopmentError(_("План уже закрыт"))
    if not reason.strip():
        raise DevelopmentError(_("Укажите основание отмены"))

    plan.status = PlanStatus.CANCELLED
    plan.summary = reason.strip()
    plan.updated_by = actor
    plan.save(update_fields=["status", "summary", "updated_by", "updated_at"])
    return plan


# ---------------------------------------------------------------------------
# Мероприятия
# ---------------------------------------------------------------------------
def complete_activity(
    activity: DevelopmentActivity,
    *,
    actor: User,
    result: str = "",
    on_date: dt.date | None = None,
) -> DevelopmentActivity:
    """Отмечает мероприятие выполненным."""
    if activity.status == ActivityStatus.COMPLETED:
        raise DevelopmentError(_("Мероприятие уже выполнено"))
    if activity.status == ActivityStatus.CANCELLED:
        raise DevelopmentError(_("Мероприятие отменено"))

    if _requires_evidence(activity) and not activity.has_evidence:
        raise DevelopmentError(
            _("Для этого вида мероприятия требуется подтверждение: приложите документ")
        )

    activity.complete(on_date=on_date, result=result)

    plan = activity.plan
    if plan.supervisor is not None:
        notify(
            plan.supervisor,
            NotificationKind.ACTIVITY_COMPLETED,
            title=str(_("Выполнено мероприятие: %(title)s") % {"title": activity.title}),
            body=str(
                _("Выполнение плана — %(rate)s %%, сотрудник — %(name)s")
                % {"rate": plan.completion_rate, "name": plan.employee.short_name}
            ),
            obj=activity,
            actor=actor,
        )
    return activity


#: Виды развития, при которых подтверждение выполнения обязательно.
EVIDENCE_REQUIRED_TYPES: frozenset[str] = frozenset(
    {ActivityType.TRAINING, ActivityType.COURSE, ActivityType.CONFERENCE}
)


def _requires_evidence(activity: DevelopmentActivity) -> bool:
    return activity.activity_type in EVIDENCE_REQUIRED_TYPES


@transaction.atomic
def add_evidence(
    activity: DevelopmentActivity,
    *,
    actor: User,
    kind: str = EvidenceKind.REPORT,
    title: str = "",
    file: Any | None = None,
    external_url: str = "",
    comment: str = "",
) -> ActivityEvidence:
    """Прикладывает подтверждение выполнения мероприятия."""
    if file is None and not external_url.strip():
        raise DevelopmentError(_("Приложите файл или укажите ссылку на подтверждение"))

    evidence = ActivityEvidence.objects.create(
        activity=activity,
        kind=kind,
        title=(title or activity.title)[:255],
        file=file,
        external_url=external_url.strip(),
        comment=comment,
        submitted_by=actor,
    )
    logger.info(
        "Приложено подтверждение",
        extra={"activity_id": str(activity.public_id), "kind": kind},
    )
    return evidence


def verify_evidence(evidence: ActivityEvidence, *, actor: User) -> ActivityEvidence:
    """Отмечает подтверждение проверенным."""
    if evidence.submitted_by_id == actor.pk:
        raise DevelopmentError(_("Подтверждение проверяет не тот, кто его приложил"))
    if actor.role not in {*PRIVILEGED_ROLES, Role.MANAGER, Role.MENTOR}:
        raise DevelopmentError(_("Проверка подтверждений доступна руководителю и наставнику"))

    evidence.is_verified = True
    evidence.verified_by = actor
    evidence.verified_at = timezone.now()
    evidence.save(update_fields=["is_verified", "verified_by", "verified_at", "updated_at"])
    return evidence


def overdue_activities(*, user: User | None = None) -> QuerySet[DevelopmentActivity]:
    """Просроченные мероприятия в границах видимости пользователя."""
    queryset = DevelopmentActivity.objects.overdue().with_related()
    if user is not None:
        from core.permissions import apply_scope_by_path

        queryset = apply_scope_by_path(queryset, user, prefix="plan__employee")
    return queryset.order_by("due_date")


def upcoming_activities(*, user: User | None = None) -> QuerySet[DevelopmentActivity]:
    """Мероприятия с приближающимся сроком."""
    queryset = DevelopmentActivity.objects.due_soon().with_related()
    if user is not None:
        from core.permissions import apply_scope_by_path

        queryset = apply_scope_by_path(queryset, user, prefix="plan__employee")
    return queryset.order_by("due_date")


# ---------------------------------------------------------------------------
# Наставничество
# ---------------------------------------------------------------------------
@transaction.atomic
def start_mentorship(
    *,
    mentor: User,
    mentee: Employee,
    goal: str,
    actor: User,
    plan: DevelopmentPlan | None = None,
    end_date: dt.date | None = None,
    frequency_days: int = 14,
) -> Mentorship:
    """Создаёт наставническую пару."""
    if mentee.user_id is not None and mentee.user_id == mentor.pk:
        raise DevelopmentError(_("Наставник и подопечный не могут быть одним человеком"))

    existing = Mentorship.objects.filter(
        mentor=mentor,
        mentee=mentee,
        status__in=[MentorshipStatus.PROPOSED, MentorshipStatus.ACTIVE, MentorshipStatus.PAUSED],
    ).first()
    if existing is not None:
        raise DevelopmentError(_("Такая пара наставничества уже существует"))

    pair = Mentorship.objects.create(
        mentor=mentor,
        mentee=mentee,
        plan=plan,
        status=MentorshipStatus.ACTIVE,
        goal=goal,
        start_date=timezone.localdate(),
        end_date=end_date,
        meeting_frequency_days=frequency_days,
    )

    notify(
        mentor,
        NotificationKind.MENTORSHIP_ASSIGNED,
        title=str(_("Назначено наставничество: %(name)s") % {"name": mentee.short_name}),
        body=goal,
        obj=pair,
        actor=actor,
    )
    if mentee.user is not None:
        notify(
            mentee.user,
            NotificationKind.MENTORSHIP_ASSIGNED,
            title=str(_("Назначен наставник: %(name)s") % {"name": mentor.short_name}),
            body=goal,
            obj=pair,
            actor=actor,
        )

    logger.info(
        "Создана пара наставничества",
        extra={"mentorship_id": str(pair.public_id), "mentor_id": mentor.pk},
    )
    return pair


def log_meeting(
    pair: Mentorship,
    *,
    actor: User,
    on_date: dt.date | None = None,
    note: str = "",
) -> Mentorship:
    """Отмечает проведённую встречу наставника с подопечным."""
    if pair.status != MentorshipStatus.ACTIVE:
        raise DevelopmentError(_("Встречи отмечаются только по действующей паре"))

    meeting_date = on_date or timezone.localdate()
    if meeting_date > timezone.localdate():
        raise DevelopmentError(_("Дата встречи не может быть в будущем"))

    pair.last_meeting_at = meeting_date
    pair.meetings_count += 1
    if note:
        pair.result = f"{pair.result}\n{meeting_date:%d.%m.%Y}: {note}".strip()
    pair.save(update_fields=["last_meeting_at", "meetings_count", "result", "updated_at"])
    return pair


def complete_mentorship(
    pair: Mentorship,
    *,
    actor: User,
    result: str,
    effectiveness: int | None = None,
) -> Mentorship:
    """Завершает наставничество с фиксацией результата."""
    if pair.status not in {MentorshipStatus.ACTIVE, MentorshipStatus.PAUSED}:
        raise DevelopmentError(_("Пара уже закрыта"))
    if not result.strip():
        raise DevelopmentError(_("Укажите результат наставничества"))

    pair.status = MentorshipStatus.COMPLETED
    pair.actual_end_date = timezone.localdate()
    pair.result = result.strip()
    if effectiveness is not None:
        pair.effectiveness = effectiveness
    pair.save(
        update_fields=[
            "status",
            "actual_end_date",
            "result",
            "effectiveness",
            "updated_at",
        ]
    )
    return pair


def mentorships_for(user: User) -> QuerySet[Mentorship]:
    """Пары наставничества, доступные пользователю."""
    from core.permissions import apply_scope_by_path

    queryset = Mentorship.objects.with_related()
    if user.role == Role.MENTOR:
        return queryset.for_mentor(user.pk)
    return apply_scope_by_path(queryset, user, prefix="mentee")


@dataclass(slots=True)
class MentorLoad:
    """Нагрузка и результативность наставника."""

    mentor: Any
    active: int = 0
    completed: int = 0
    average_effectiveness: float = 0.0
    meetings: int = 0


def mentor_load() -> list[MentorLoad]:
    """Сводка по наставникам: нагрузка, число встреч, средняя оценка."""
    from django.db.models import Sum

    from apps.accounts.models import User as UserModel

    rows = (
        UserModel.objects.filter(mentorships_as_mentor__isnull=False)
        .annotate(
            active_pairs=Count(
                "mentorships_as_mentor",
                filter=Q(mentorships_as_mentor__status=MentorshipStatus.ACTIVE),
                distinct=True,
            ),
            completed_pairs=Count(
                "mentorships_as_mentor",
                filter=Q(mentorships_as_mentor__status=MentorshipStatus.COMPLETED),
                distinct=True,
            ),
            effectiveness_avg=Avg("mentorships_as_mentor__effectiveness"),
            meetings_total=Sum("mentorships_as_mentor__meetings_count"),
        )
        .order_by("last_name")
        .distinct()
    )

    return [
        MentorLoad(
            mentor=row,
            active=row.active_pairs,
            completed=row.completed_pairs,
            average_effectiveness=round(row.effectiveness_avg or 0, 2),
            meetings=row.meetings_total or 0,
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Библиотека развития
# ---------------------------------------------------------------------------
def resources_for_competency(competency_id: int, level: int | None = None):
    """Учебные ресурсы, развивающие компетенцию до указанного уровня."""
    queryset = LearningResource.objects.filter(is_active=True, competencies=competency_id)
    if level is not None:
        queryset = queryset.filter(target_level_min__lte=level, target_level_max__gte=level)
    return queryset.order_by("-rating", "name")


@dataclass(slots=True)
class LibraryStatistics:
    """Сводка по библиотеке развития."""

    total: int = 0
    internal: int = 0
    by_format: list[tuple[str, int]] = field(default_factory=list)
    average_rating: float = 0.0
    uncovered_competencies: int = 0


def library_statistics() -> LibraryStatistics:
    """Сводка по библиотеке с числом компетенций, не обеспеченных ни одним ресурсом."""
    from apps.competencies.models import Competency

    formats = dict(LearningResource._meta.get_field("resource_format").choices or [])
    aggregate = LearningResource.objects.filter(is_active=True).aggregate(
        total=Count("id"),
        internal=Count("id", filter=Q(is_internal=True)),
        rating=Avg("rating"),
    )

    by_format = [
        (str(formats.get(row["resource_format"], row["resource_format"])), row["count"])
        for row in LearningResource.objects.filter(is_active=True)
        .values("resource_format")
        .annotate(count=Count("id"))
        .order_by("-count")
    ]

    uncovered = (
        Competency.objects.filter(is_active=True)
        .exclude(learning_resources__is_active=True)
        .count()
    )

    return LibraryStatistics(
        total=aggregate["total"] or 0,
        internal=aggregate["internal"] or 0,
        by_format=by_format,
        average_rating=round(aggregate["rating"] or 0, 2),
        uncovered_competencies=uncovered,
    )
