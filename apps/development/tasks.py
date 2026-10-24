"""Фоновые задачи раздела развития."""

from __future__ import annotations

import datetime as dt

from celery import shared_task

from django.utils import timezone
from django.utils.translation import gettext as _

from apps.development.models import (
    ActivityStatus,
    DevelopmentActivity,
    Mentorship,
    MentorshipStatus,
    PlanStatus,
)
from apps.notifications.models import NotificationKind
from apps.notifications.services import notify
from core.logging import get_logger
from core.settings_types import get_reservix_settings

logger = get_logger("development")


@shared_task
def mark_overdue_activities() -> int:
    """Проставляет признак просрочки мероприятиям с истёкшим сроком."""
    today = timezone.localdate()
    changed = DevelopmentActivity.objects.filter(
        due_date__lt=today,
        status__in=[ActivityStatus.PENDING, ActivityStatus.IN_PROGRESS],
    ).update(status=ActivityStatus.OVERDUE, updated_at=timezone.now())

    if changed:
        logger.info("Отмечены просроченные мероприятия", extra={"count": changed})
    return changed


@shared_task
def notify_due_soon() -> int:
    """Извещает исполнителей о приближении срока мероприятия."""
    options = get_reservix_settings()
    today = timezone.localdate()
    horizon = today + dt.timedelta(days=options["ACTIVITY_DUE_SOON_DAYS"])

    activities = DevelopmentActivity.objects.filter(
        due_date__gte=today,
        due_date__lte=horizon,
        status__in=[ActivityStatus.PENDING, ActivityStatus.IN_PROGRESS],
        plan__status=PlanStatus.ACTIVE,
    ).select_related("plan", "plan__employee", "plan__supervisor", "responsible")

    sent = 0
    for activity in activities:
        recipient = activity.responsible or activity.plan.supervisor
        if recipient is None:
            logger.debug(
                "У мероприятия нет адресата напоминания",
                extra={"activity_id": str(activity.public_id)},
            )
            continue

        notification = notify(
            recipient,
            NotificationKind.ACTIVITY_DUE_SOON,
            title=_("Приближается срок: %(title)s") % {"title": activity.title},
            body=_("Срок выполнения — %(date)s. %(plan)s.")
            % {"date": f"{activity.due_date:%d.%m.%Y}", "plan": activity.plan.title},
            obj=activity,
        )
        sent += int(notification is not None)

    logger.info("Разосланы напоминания о сроках", extra={"count": sent})
    return sent


@shared_task
def notify_overdue() -> int:
    """Извещает куратора плана о просроченных мероприятиях подчинённых."""
    today = timezone.localdate()
    activities = DevelopmentActivity.objects.filter(
        due_date__lt=today,
        status__in=[ActivityStatus.PENDING, ActivityStatus.IN_PROGRESS, ActivityStatus.OVERDUE],
        plan__status=PlanStatus.ACTIVE,
        plan__supervisor__isnull=False,
    ).select_related("plan", "plan__employee", "plan__supervisor")

    sent = 0
    for activity in activities:
        supervisor = activity.plan.supervisor
        if supervisor is None:  # pragma: no cover — отсечено условием отбора
            continue
        notification = notify(
            supervisor,
            NotificationKind.ACTIVITY_OVERDUE,
            title=_("Просрочено мероприятие: %(title)s") % {"title": activity.title},
            body=_("Срок истёк %(date)s, сотрудник — %(name)s")
            % {
                "date": f"{activity.due_date:%d.%m.%Y}",
                "name": activity.plan.employee.short_name,
            },
            obj=activity,
        )
        sent += int(notification is not None)

    logger.info("Разосланы извещения о просрочке", extra={"count": sent})
    return sent


@shared_task
def remind_mentorship_meetings() -> int:
    """Напоминает наставникам о наступившем сроке встречи."""
    today = timezone.localdate()
    pairs = Mentorship.objects.filter(status=MentorshipStatus.ACTIVE).select_related(
        "mentor", "mentee"
    )

    sent = 0
    for pair in pairs:
        due = pair.last_meeting_at is None or (
            pair.last_meeting_at + dt.timedelta(days=pair.meeting_frequency_days) <= today
        )
        if not due:
            continue

        notification = notify(
            pair.mentor,
            NotificationKind.MENTORSHIP_ASSIGNED,
            title=_("Пора встретиться: %(name)s") % {"name": pair.mentee.short_name},
            body=(
                _("Встреча по плану наставничества не проводилась с %(date)s.")
                % {"date": f"{pair.last_meeting_at:%d.%m.%Y}"}
                if pair.last_meeting_at
                else _("Встреча по плану наставничества не проводилась ни разу с начала пары.")
            ),
            obj=pair,
        )
        sent += int(notification is not None)

    logger.info("Разосланы напоминания о встречах", extra={"count": sent})
    return sent


@shared_task
def notify_expiring_plans(days: int = 30) -> int:
    """Извещает кураторов о планах, срок которых истекает."""
    from apps.development.models import DevelopmentPlan

    today = timezone.localdate()
    horizon = today + dt.timedelta(days=days)

    plans = DevelopmentPlan.objects.filter(
        status=PlanStatus.ACTIVE,
        end_date__gte=today,
        end_date__lte=horizon,
        supervisor__isnull=False,
    ).select_related("employee", "supervisor")

    sent = 0
    for plan in plans:
        supervisor = plan.supervisor
        if supervisor is None:  # pragma: no cover — отсечено условием отбора
            continue
        notification = notify(
            supervisor,
            NotificationKind.PLAN_ASSIGNED,
            title=_("Истекает срок плана развития: %(name)s") % {"name": plan.employee.short_name},
            body=_(
                "План «%(plan)s» завершается %(date)s. Выполнение — %(rate)s %%. "
                "Требуется подведение итога."
            )
            % {
                "plan": plan.title,
                "date": f"{plan.end_date:%d.%m.%Y}",
                "rate": plan.completion_rate,
            },
            obj=plan,
        )
        sent += int(notification is not None)

    logger.info("Разосланы извещения об истекающих планах", extra={"count": sent})
    return sent
