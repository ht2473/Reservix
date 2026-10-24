"""Стартовые страницы рабочих мест."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView

from apps.analytics.models import NineBoxPlacement
from apps.analytics.services import reserve_health
from apps.assessment.models import AssessmentSession, SessionStatus
from apps.assessment.services import campaigns_due_soon, pending_sessions_for
from apps.competencies.services import build_profile, resolve_competency_model
from apps.development.models import ActivityStatus, DevelopmentActivity, DevelopmentPlan, PlanStatus
from apps.development.services import (
    development_summary,
    mentorships_for,
    overdue_activities,
    upcoming_activities,
)
from apps.people.models import Employee
from apps.people.views import people_summary
from apps.reserve.models import ReserveEntry
from apps.reserve.services import pending_for_user, reserve_funnel, reserve_summary
from apps.succession.services import coverage_report
from core.constants import Role
from core.mixins import BreadcrumbMixin, PageMetaMixin, RoleRequiredMixin
from core.permissions import apply_employee_scope, apply_scope_by_path, current_user

if TYPE_CHECKING:
    from core.breadcrumbs import BreadcrumbTrail

#: Горизонт «ближайших» сроков на дашборде.
HORIZON_DAYS = 14


class DashboardBaseMixin(RoleRequiredMixin, PageMetaMixin, BreadcrumbMixin):
    """Общее поведение стартовых страниц: доступ, заголовки, цепочка навигации."""

    page_section = "dashboard"
    permission_denied_message = _("Раздел недоступен для вашей роли")

    def get_breadcrumbs(self, trail: BreadcrumbTrail) -> BreadcrumbTrail:
        # Крошка страницы не добавляется: дашборд и есть корень рабочего места,
        # и звено «Рабочее место → Дашборд», ведущее само на себя, — шум.
        return trail


# ---------------------------------------------------------------------------
# Кадровая служба
# ---------------------------------------------------------------------------
class HrDashboardView(DashboardBaseMixin, TemplateView):
    """Рабочее место кадровой службы."""

    template_name = "dashboard/hr.html"
    allowed_roles = (Role.HR, Role.ADMIN)
    page_title = _("Дашборд кадровой службы")
    page_subtitle = _("Состояние резерва, очередь решений и ближайшие сроки")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        user = current_user(self.request)
        today = timezone.localdate()

        # Отчёт об обеспеченности считается один раз и передаётся в расчёт показателей здоровья
        # резерва.
        coverage = coverage_report()
        health = reserve_health(user=user, succession_coverage=coverage)
        development = development_summary(user=user)
        reserve = reserve_summary(user=user)
        people = people_summary(user)

        context["health"] = health
        context["development"] = development
        context["reserve"] = reserve
        context["coverage"] = coverage
        context["people"] = people
        context["funnel"] = reserve_funnel(user=user)

        # Очередь работы.
        context["queue"] = [
            {
                "label": _("Заявки на рассмотрении"),
                "count": reserve.pending_nominations,
                "url": f"{reverse('reserve:nominations')}?status=on_hr",
                "hint": _("Выдвижения, прошедшие согласование руководителя"),
            },
            {
                "label": _("Планы развития на утверждении"),
                "count": development.on_approval,
                "url": f"{reverse('development:list')}?status=on_approval",
                "hint": _("Планы, ожидающие решения кадровой службы"),
            },
            {
                "label": _("Просрочен пересмотр резерва"),
                "count": reserve.overdue_reviews,
                "url": reverse("reserve:reviews"),
                "hint": _("Записи, по которым наступил срок очередного пересмотра"),
            },
            {
                "label": _("Выполнено без подтверждения"),
                "count": development.unconfirmed,
                "url": f"{reverse('development:activities')}?status=completed",
                "hint": _("Мероприятия закрыты, но документ не приложен"),
            },
            {
                "label": _("Критичные должности без преемников"),
                "count": len(coverage.uncovered),
                "url": reverse("succession:coverage"),
                "hint": _("План преемственности не составлен или пуст"),
            },
        ]

        # Перечень непокрытых должностей берётся из уже собранного отчёта, а не запрашивается
        # заново: отчёт обходит те же записи и складывает их в `uncovered` по ходу подсчёта.
        context["uncovered"] = coverage.uncovered[:6]
        context["campaigns"] = campaigns_due_soon()[:5]
        context["overdue_activities"] = (
            overdue_activities(user=user)
            .select_related("plan", "plan__employee")
            .order_by("due_date")[:8]
        )
        context["recent_placements"] = (
            apply_scope_by_path(NineBoxPlacement.objects.current(), user, prefix="employee")
            .select_related("employee", "employee__department")
            .order_by("-created_at")[:6]
        )
        context["top_entries"] = (
            apply_scope_by_path(ReserveEntry.objects.in_reserve(), user, prefix="employee")
            .with_related()
            .exclude(readiness_index=None)
            .order_by("-readiness_index")[:8]
        )
        context["today"] = today
        context["horizon"] = today + dt.timedelta(days=HORIZON_DAYS)
        return context


# ---------------------------------------------------------------------------
# Руководитель
# ---------------------------------------------------------------------------
class ManagerDashboardView(DashboardBaseMixin, TemplateView):
    """Рабочее место руководителя подразделения."""

    template_name = "dashboard/manager.html"
    allowed_roles = (Role.MANAGER,)
    page_title = _("Дашборд подразделения")
    page_subtitle = _("Что требует вашего решения, и как готовится команда")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        user = current_user(self.request)
        today = timezone.localdate()

        nominations = pending_for_user(user)
        sessions = pending_sessions_for(user)
        mentorships = mentorships_for(user)
        development = development_summary(user=user)
        people = people_summary(user)

        context["people"] = people
        context["development"] = development
        context["reserve"] = reserve_summary(user=user)

        context["queue"] = [
            {
                "label": _("Заявки на согласование"),
                "count": nominations.count(),
                "url": reverse("reserve:approvals"),
                "hint": _("Выдвижения сотрудников вашего подразделения"),
            },
            {
                "label": _("Анкеты оценки к заполнению"),
                "count": sessions.count(),
                "url": reverse("assessment:inbox"),
                "hint": _("Сессии оценки, где вы указаны оценивающим"),
            },
            {
                "label": _("Просроченные мероприятия"),
                "count": development.overdue_activities,
                "url": f"{reverse('development:activities')}?scope=overdue",
                "hint": _("Мероприятия подчинённых с истёкшим сроком"),
            },
            {
                "label": _("Наступил срок встречи"),
                "count": development.meetings_due,
                "url": reverse("development:mentorship"),
                "hint": _("Пары наставничества без встречи в установленном ритме"),
            },
        ]

        context["nominations"] = nominations.select_related("employee", "target_position")[:6]
        context["sessions"] = sessions.select_related("employee", "campaign")[:6]
        context["mentorships"] = mentorships.select_related("mentor", "mentee")[:5]

        team = apply_employee_scope(Employee.objects.active(), user).with_org()
        context["team_size"] = team.count()
        context["without_plan"] = team.exclude(
            development_plans__status__in=(PlanStatus.ACTIVE, PlanStatus.ON_APPROVAL)
        ).count()
        context["at_risk"] = team.at_risk().order_by("last_name")[:6]

        context["entries"] = (
            apply_scope_by_path(ReserveEntry.objects.in_reserve(), user, prefix="employee")
            .with_related()
            .order_by("-readiness_index")[:8]
        )
        context["upcoming"] = (
            upcoming_activities(user=user)
            .select_related("plan", "plan__employee")
            .order_by("due_date")[:8]
        )
        context["today"] = today
        return context


# ---------------------------------------------------------------------------
# Сотрудник
# ---------------------------------------------------------------------------
class EmployeeDashboardView(DashboardBaseMixin, TemplateView):
    """Личный кабинет сотрудника."""

    template_name = "dashboard/employee.html"
    allowed_roles = (Role.EMPLOYEE, Role.MENTOR, Role.MANAGER, Role.HR, Role.ADMIN)
    page_title = _("Мой кабинет")
    page_subtitle = _("Развитие, оценки и участие в кадровом резерве")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        user = current_user(self.request)
        employee = getattr(user, "employee", None)
        context["employee"] = employee

        if employee is None:
            return context

        today = timezone.localdate()

        plan = (
            DevelopmentPlan.objects.filter(employee=employee)
            .exclude(status=PlanStatus.CANCELLED)
            .select_related("entry", "entry__target_position")
            .order_by("-start_date")
            .first()
        )
        context["plan"] = plan
        context["activities"] = (
            DevelopmentActivity.objects.filter(
                plan=plan,
                status__in=(ActivityStatus.PENDING, ActivityStatus.IN_PROGRESS),
            )
            .select_related("competency")
            .order_by("due_date")[:6]
            if plan is not None
            else []
        )

        entries = (
            ReserveEntry.objects.filter(employee=employee)
            .select_related("target_position", "target_position__grade")
            .order_by("-readiness_index")
        )
        context["entries"] = entries
        context["nominations"] = employee.nominations.select_related("target_position").order_by(
            "-created_at"
        )[:3]

        # Разрывы строятся по требованиям целевой должности записи резерва, а при её отсутствии —
        # по требованиям занимаемой должности.
        target = entries.first()
        competency_model = None
        if target is not None:
            position = target.target_position
            competency_model = resolve_competency_model(
                position_id=position.pk, grade_id=position.grade_id
            )
        profile = build_profile(employee, competency_model)
        context["profile"] = profile
        context["target_entry"] = target
        # Приоритеты развития, а не первые попавшиеся требования: список упорядочен по величине
        # недобора с учётом веса требования, и первые шесть строк — это то, чем действительно стоит
        # заняться.
        context["gaps"] = profile.development_priorities(limit=6)

        context["sessions"] = (
            AssessmentSession.objects.filter(employee=employee, status=SessionStatus.APPROVED)
            .select_related("campaign")
            .order_by("-submitted_at")[:5]
        )
        context["placement"] = (
            NineBoxPlacement.objects.filter(employee=employee).order_by("-created_at").first()
        )
        context["mentorships"] = mentorships_for(user).select_related("mentor", "mentee")[:3]
        context["today"] = today
        return context
