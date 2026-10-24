"""Модульные тесты фоновых задач раздела развития."""

from __future__ import annotations

import datetime as dt

import pytest

from django.utils import timezone

from apps.development.models import ActivityStatus, MentorshipStatus, PlanStatus
from apps.development.tasks import (
    mark_overdue_activities,
    notify_due_soon,
    notify_expiring_plans,
    notify_overdue,
    remind_mentorship_meetings,
)
from apps.notifications.models import Notification, NotificationKind
from tests.factories.domain import (
    DevelopmentActivityFactory,
    DevelopmentPlanFactory,
    EmployeeFactory,
    MentorshipFactory,
)

pytestmark = [pytest.mark.unit, pytest.mark.django_db]


@pytest.fixture
def plan(department, manager_user):
    """Действующий план с куратором."""
    return DevelopmentPlanFactory(
        employee=EmployeeFactory(department=department),
        supervisor=manager_user,
        status=PlanStatus.ACTIVE,
    )


class TestOverdueMarking:
    """Проставление признака просрочки."""

    def test_истёкшие_мероприятия_помечаются(self, plan):
        overdue = DevelopmentActivityFactory(
            plan=plan,
            due_date=timezone.localdate() - dt.timedelta(days=3),
            status=ActivityStatus.PENDING,
        )
        upcoming = DevelopmentActivityFactory(
            plan=plan,
            due_date=timezone.localdate() + dt.timedelta(days=3),
            status=ActivityStatus.PENDING,
        )

        assert mark_overdue_activities() == 1

        overdue.refresh_from_db()
        upcoming.refresh_from_db()
        assert overdue.status == ActivityStatus.OVERDUE
        assert upcoming.status == ActivityStatus.PENDING

    def test_выполненное_мероприятие_не_трогается(self, plan):
        """
        Выполненное задним числом мероприятие остаётся выполненным: срок к нему уже неприменим.
        """
        done = DevelopmentActivityFactory(
            plan=plan,
            due_date=timezone.localdate() - dt.timedelta(days=10),
            status=ActivityStatus.COMPLETED,
            completion_date=timezone.localdate() - dt.timedelta(days=12),
        )
        assert mark_overdue_activities() == 0

        done.refresh_from_db()
        assert done.status == ActivityStatus.COMPLETED


class TestReminders:
    """Выбор адресата напоминания."""

    def test_о_приближении_срока_извещается_исполнитель(self, plan, employee_user):
        DevelopmentActivityFactory(
            plan=plan,
            due_date=timezone.localdate() + dt.timedelta(days=3),
            status=ActivityStatus.PENDING,
            responsible=employee_user,
        )

        assert notify_due_soon() == 1
        notification = Notification.objects.get()
        assert notification.recipient == employee_user
        assert notification.kind == NotificationKind.ACTIVITY_DUE_SOON

    def test_без_исполнителя_извещается_куратор(self, plan, manager_user):
        DevelopmentActivityFactory(
            plan=plan,
            due_date=timezone.localdate() + dt.timedelta(days=3),
            status=ActivityStatus.PENDING,
            responsible=None,
        )

        assert notify_due_soon() == 1
        assert Notification.objects.get().recipient == manager_user

    def test_о_просрочке_извещается_куратор(self, plan, employee_user, manager_user):
        """
        Адресат иной, чем у напоминания о приближении срока: о пропущенном сроке нужно знать тому,
        кто отвечает за выполнение плана.
        """
        DevelopmentActivityFactory(
            plan=plan,
            due_date=timezone.localdate() - dt.timedelta(days=5),
            status=ActivityStatus.PENDING,
            responsible=employee_user,
        )

        assert notify_overdue() == 1
        notification = Notification.objects.get()
        assert notification.recipient == manager_user
        assert notification.kind == NotificationKind.ACTIVITY_OVERDUE

    def test_мероприятия_черновика_не_напоминают_о_себе(self, department, manager_user):
        """
        План, ещё не введённый в действие, обязательств не создаёт.
        """
        draft = DevelopmentPlanFactory(
            employee=EmployeeFactory(department=department),
            supervisor=manager_user,
            status=PlanStatus.DRAFT,
        )
        DevelopmentActivityFactory(
            plan=draft,
            due_date=timezone.localdate() + dt.timedelta(days=2),
            status=ActivityStatus.PENDING,
        )
        assert notify_due_soon() == 0


class TestMentorshipReminders:
    """Напоминания о встречах наставника."""

    def test_напоминание_при_наступившем_сроке(self, mentor_user, department):
        MentorshipFactory(
            mentor=mentor_user,
            mentee=EmployeeFactory(department=department),
            status=MentorshipStatus.ACTIVE,
            meeting_frequency_days=14,
            last_meeting_at=timezone.localdate() - dt.timedelta(days=20),
        )
        assert remind_mentorship_meetings() == 1
        assert Notification.objects.get().recipient == mentor_user

    def test_пара_без_встреч_напоминает_сразу(self, mentor_user, department):
        MentorshipFactory(
            mentor=mentor_user,
            mentee=EmployeeFactory(department=department),
            status=MentorshipStatus.ACTIVE,
            last_meeting_at=None,
        )
        assert remind_mentorship_meetings() == 1

    def test_недавняя_встреча_напоминания_не_вызывает(self, mentor_user, department):
        MentorshipFactory(
            mentor=mentor_user,
            mentee=EmployeeFactory(department=department),
            status=MentorshipStatus.ACTIVE,
            meeting_frequency_days=30,
            last_meeting_at=timezone.localdate() - dt.timedelta(days=3),
        )
        assert remind_mentorship_meetings() == 0

    def test_завершённая_пара_не_напоминает(self, mentor_user, department):
        MentorshipFactory(
            mentor=mentor_user,
            mentee=EmployeeFactory(department=department),
            status=MentorshipStatus.COMPLETED,
            last_meeting_at=None,
        )
        assert remind_mentorship_meetings() == 0


class TestExpiringPlans:
    """Извещение об истекающем сроке плана."""

    def test_план_с_близким_окончанием_попадает_в_рассылку(self, department, manager_user):
        DevelopmentPlanFactory(
            employee=EmployeeFactory(department=department),
            supervisor=manager_user,
            status=PlanStatus.ACTIVE,
            start_date=timezone.localdate() - dt.timedelta(days=300),
            end_date=timezone.localdate() + dt.timedelta(days=10),
        )
        assert notify_expiring_plans() == 1
        assert Notification.objects.get().recipient == manager_user

    def test_план_с_дальним_сроком_не_беспокоит(self, plan):
        plan.end_date = timezone.localdate() + dt.timedelta(days=200)
        plan.save(update_fields=["end_date"])
        assert notify_expiring_plans() == 0
