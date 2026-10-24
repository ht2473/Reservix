"""Представления раздела сотрудников."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.generic import DetailView, ListView
from django.views.generic.edit import UpdateView

from apps.analytics.models import NineBoxPlacement
from apps.assessment.models import AssessmentSession, SessionStatus
from apps.development.models import DevelopmentPlan, PlanStatus
from apps.documents.services import visible_documents
from apps.people.forms import EmployeeCardForm, EmployeeFilterForm
from apps.people.models import AttritionRisk, Employee, EmployeeStatus
from apps.reserve.models import ReserveEntry
from core.constants import Role
from core.mixins import (
    AuditedFormMixin,
    BreadcrumbMixin,
    FilterMixin,
    PageMetaMixin,
    RoleRequiredMixin,
    SortableMixin,
    SuccessMessageMixin,
    WindowedPaginationMixin,
)
from core.permissions import (
    apply_employee_scope,
    can_edit_employee,
    can_view_employee,
    current_user,
)

if TYPE_CHECKING:
    from django.db.models import QuerySet

#: Роли, ведущие кадровый справочник целиком.
CURATOR_ROLES: tuple[str, ...] = (Role.ADMIN, Role.HR)

#: Роли, которым доступен раздел сотрудников.
PEOPLE_ROLES: tuple[str, ...] = (Role.ADMIN, Role.HR, Role.MANAGER)

#: Срок, после которого оценка перестаёт считаться свежей.
ASSESSMENT_FRESHNESS_DAYS = 365


class PeopleBaseMixin(RoleRequiredMixin, PageMetaMixin, BreadcrumbMixin):
    """Общее поведение страниц раздела: доступ, заголовки, цепочка навигации."""

    allowed_roles = PEOPLE_ROLES
    page_section = "people"
    permission_denied_message = _("Раздел сотрудников недоступен для вашей роли")

    def get_breadcrumbs(self, trail):
        root = "people:list" if self.request.user.role in CURATOR_ROLES else "people:team"
        trail.add(_("Сотрудники"), root)
        # Берётся вычисленный заголовок, а не объявленный атрибутом: на карточке он равен фамилии
        # сотрудника, и цепочка «Сотрудники → Карточка сотрудника» при заголовке «Агафонов М.
        title = self.get_page_title()
        if title and self.request.resolver_match.url_name not in {"list", "team"}:
            trail.add(title)
        return trail

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_section_label"] = _("Сотрудники")
        context["is_curator"] = self.request.user.role in CURATOR_ROLES
        return context


# ---------------------------------------------------------------------------
# Список сотрудников
# ---------------------------------------------------------------------------
class EmployeeListView(
    PeopleBaseMixin,
    FilterMixin,
    SortableMixin,
    WindowedPaginationMixin,
    ListView,
):
    """Кадровый справочник организации."""

    template_name = "people/index.html"
    model = Employee
    context_object_name = "employees"
    paginate_by = 25
    allowed_roles = PEOPLE_ROLES
    page_title = _("Сотрудники")
    page_subtitle = _("Кадровый справочник: состав, оценка, участие в резерве")
    filter_form_class = EmployeeFilterForm
    sort_fields = {
        "name": "last_name",
        "department": "department__name",
        "position": "position__name",
        "hired": "hire_date",
    }
    default_sort = "name"

    def get_queryset(self) -> QuerySet[Employee]:
        # Область видимости применяется до фильтра формы: иначе отбор по подразделению сужал бы
        # неограниченный набор и возвращал сотрудников, которых пользователь видеть не должен.
        queryset = apply_employee_scope(
            Employee.objects.with_org().with_user(),
            current_user(self.request),
        ).annotate(reserve_count=Count("reserve_entries", distinct=True))
        form = self.get_filter_form()
        if form is not None and form.is_valid():
            queryset = form.filter(queryset)
        self._filter_form = form
        return self.apply_sort(queryset)

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["summary"] = people_summary(current_user(self.request))
        context["statuses"] = EmployeeStatus.choices
        return context


class TeamView(PeopleBaseMixin, WindowedPaginationMixin, ListView):
    """Команда руководителя."""

    template_name = "people/team.html"
    model = Employee
    context_object_name = "employees"
    paginate_by = 50
    allowed_roles = (Role.MANAGER, Role.ADMIN, Role.HR)
    page_title = _("Мои сотрудники")
    page_subtitle = _("Состав подразделения: оценка, резерв, развитие, риски")

    def get_queryset(self) -> QuerySet[Employee]:
        queryset = apply_employee_scope(
            Employee.objects.active().with_org().with_user(),
            current_user(self.request),
        )
        return queryset.annotate(
            reserve_count=Count("reserve_entries", distinct=True),
            plan_count=Count(
                "development_plans",
                filter=Q(development_plans__status=PlanStatus.ACTIVE),
                distinct=True,
            ),
        ).order_by("last_name", "first_name")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["summary"] = people_summary(current_user(self.request))
        context["stale_days"] = ASSESSMENT_FRESHNESS_DAYS
        context["assessed_ids"] = recently_assessed_ids(context["employees"])
        return context


# ---------------------------------------------------------------------------
# Карточка сотрудника
# ---------------------------------------------------------------------------
class EmployeeDetailView(PeopleBaseMixin, DetailView):
    """Карточка сотрудника."""

    template_name = "people/detail.html"
    model = Employee
    context_object_name = "employee"
    slug_field = "public_id"
    slug_url_kwarg = "public_id"
    allowed_roles = (*PEOPLE_ROLES, Role.MENTOR, Role.EMPLOYEE)
    page_title = _("Карточка сотрудника")

    def get_queryset(self) -> QuerySet[Employee]:
        return Employee.objects.with_org().with_user().select_related("manager")

    def get_object(self, queryset: QuerySet[Employee] | None = None) -> Employee:
        employee = super().get_object(queryset)
        # Вторая проверка, помимо ограничения выборки в списке: сюда приходят по идентификатору из
        # адреса, и подстановка чужого идентификатора — ровно тот случай, ради которого объектная
        # проверка существует.
        if not can_view_employee(current_user(self.request), employee):
            raise PermissionDenied(str(_("Карточка сотрудника недоступна")))
        return employee

    def get_page_title(self) -> str:
        return self.object.short_name

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        employee = self.object
        user = current_user(self.request)

        context["page_subtitle"] = employee.title
        context["can_edit"] = can_edit_employee(user, employee)

        context["entries"] = (
            ReserveEntry.objects.filter(employee=employee)
            .select_related("target_position", "target_position__grade", "reserve_group")
            .order_by("-readiness_index")
        )
        context["plans"] = (
            DevelopmentPlan.objects.filter(employee=employee)
            .select_related("entry", "entry__target_position")
            .order_by("-start_date")[:5]
        )
        context["sessions"] = (
            AssessmentSession.objects.filter(employee=employee)
            .select_related("campaign", "assessor")
            .order_by("-created_at")[:8]
        )
        context["placement"] = (
            NineBoxPlacement.objects.filter(employee=employee)
            .select_related("session")
            .order_by("-created_at")
            .first()
        )
        context["history"] = employee.employment_history.select_related(
            "position", "department", "branch"
        ).order_by("-start_date")[:10]

        # Документы отбираются тем же правилом, что и в разделе документов: право на документ
        # выводится из права на сотрудника, а собственный уровень доступа способен круг читателей
        # только сузить.
        context["documents"] = (
            visible_documents(user).filter(employee=employee).order_by("-created_at")[:8]
        )

        context["subordinates"] = (
            Employee.objects.active().filter(manager=employee).with_org().order_by("last_name")[:12]
        )
        return context


class EmployeeCardUpdateView(
    PeopleBaseMixin,
    SuccessMessageMixin,
    AuditedFormMixin,
    UpdateView,
):
    """Правка характеристик, влияющих на расчёт готовности."""

    template_name = "people/edit.html"
    model = Employee
    form_class = EmployeeCardForm
    context_object_name = "employee"
    slug_field = "public_id"
    slug_url_kwarg = "public_id"
    allowed_roles = CURATOR_ROLES
    page_title = _("Правка карточки")
    success_message = _("Карточка сотрудника обновлена")

    def get_queryset(self) -> QuerySet[Employee]:
        return Employee.objects.with_org()

    def get_object(self, queryset: QuerySet[Employee] | None = None) -> Employee:
        employee = super().get_object(queryset)
        if not can_edit_employee(current_user(self.request), employee):
            raise PermissionDenied(str(_("Правка карточки недоступна")))
        return employee

    def get_page_title(self) -> str:
        return str(_("Правка карточки")) + ": " + self.object.short_name

    def form_valid(self, form):
        response = super().form_valid(form)
        if form.raises_succession_concern:
            messages.warning(
                self.request,
                _(
                    "Ключевой сотрудник с высоким риском ухода. Проверьте, есть ли "
                    "по его должности действующий план преемственности."
                ),
            )
        return response

    def get_success_url(self) -> str:
        return self.object.get_absolute_url()


# ---------------------------------------------------------------------------
# Сводки
# ---------------------------------------------------------------------------
def people_summary(user) -> dict[str, Any]:
    """Сводка по кадровому составу в области видимости пользователя."""
    scoped = apply_employee_scope(Employee.objects.all(), user)
    since = timezone.localdate() - dt.timedelta(days=ASSESSMENT_FRESHNESS_DAYS)

    totals: dict[str, Any] = scoped.aggregate(
        total=Count("id"),
        employed=Count(
            "id", filter=Q(status__in=(EmployeeStatus.ACTIVE, EmployeeStatus.PROBATION))
        ),
        key_talent=Count("id", filter=Q(is_key_talent=True)),
        at_risk=Count(
            "id",
            filter=Q(attrition_risk=AttritionRisk.HIGH) & ~Q(status=EmployeeStatus.DISMISSED),
        ),
        external=Count("id", filter=Q(is_external=True)),
    )

    totals["in_reserve"] = scoped.filter(reserve_entries__isnull=False).distinct().count()
    totals["assessed"] = (
        scoped.filter(
            assessment_sessions__status=SessionStatus.APPROVED,
            assessment_sessions__submitted_at__date__gte=since,
        )
        .distinct()
        .count()
    )

    employed = totals["employed"] or 0
    totals["assessed_share"] = round(totals["assessed"] / employed * 100, 1) if employed else 0.0
    return totals


def recently_assessed_ids(employees) -> set[int]:
    """Идентификаторы сотрудников списка с завершённой оценкой за последний год."""
    ids = [employee.pk for employee in employees]
    if not ids:
        return set()
    since = timezone.localdate() - dt.timedelta(days=ASSESSMENT_FRESHNESS_DAYS)
    return set(
        AssessmentSession.objects.filter(
            employee_id__in=ids,
            status=SessionStatus.APPROVED,
            submitted_at__date__gte=since,
        ).values_list("employee_id", flat=True)
    )
