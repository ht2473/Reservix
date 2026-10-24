"""Представления справочника компетенций и профилей соответствия."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db.models import Avg, Count, Q, QuerySet, Sum
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from django.views.generic import DetailView, ListView, TemplateView

from apps.assessment.aggregation import DEFAULT_WINDOW_MONTHS
from apps.competencies.forms import CompetencyFilterForm, GapMatrixFilterForm
from apps.competencies.models import (
    WEIGHT_SUM_TOLERANCE,
    Competency,
    CompetencyCategory,
    CompetencyModel,
    CompetencyRequirement,
    CompetencyScaleLevel,
)
from apps.competencies.services import (
    build_gap_matrix,
    build_profile,
    build_radar,
    model_for_employee,
)
from apps.org.models import Department
from apps.people.models import Employee
from core.constants import Role, ScopeLevel
from core.mixins import (
    BreadcrumbMixin,
    FilterMixin,
    PageMetaMixin,
    RoleRequiredMixin,
    SortableMixin,
    WindowedPaginationMixin,
)
from core.permissions import can_view_employee, current_user, is_privileged

#: Роли, которым доступен справочник компетенций.
DICTIONARY_ROLES: tuple[str, ...] = (
    Role.ADMIN,
    Role.HR,
    Role.MANAGER,
    Role.MENTOR,
    Role.EMPLOYEE,
)

#: Роли, которым доступно ведение моделей требований.
EDITOR_ROLES: tuple[str, ...] = (Role.ADMIN, Role.HR)

#: Роли, которым доступны профили соответствия чужих сотрудников.
PROFILE_ROLES: tuple[str, ...] = (Role.ADMIN, Role.HR, Role.MANAGER, Role.MENTOR)

#: Роли, которым доступна матрица разрывов подразделения.
MATRIX_ROLES: tuple[str, ...] = (Role.ADMIN, Role.HR, Role.MANAGER)


class CompetencyBaseMixin(RoleRequiredMixin, PageMetaMixin, BreadcrumbMixin):
    """Общее поведение страниц раздела: доступ, заголовки, цепочка навигации."""

    allowed_roles = DICTIONARY_ROLES
    page_section = "competencies"
    permission_denied_message = _("Раздел компетенций недоступен для вашей роли")

    def get_breadcrumbs(self, trail):
        trail.add(_("Компетенции"), "competencies:index")
        title = getattr(self, "page_title", "")
        if title and self.request.resolver_match.url_name != "index":
            trail.add(str(title))
        return trail

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_section_label"] = _("Компетенции")
        context["can_edit"] = self.request.user.role in EDITOR_ROLES
        return context


# ---------------------------------------------------------------------------
# Справочник
# ---------------------------------------------------------------------------
class CompetencyListView(
    CompetencyBaseMixin,
    FilterMixin,
    SortableMixin,
    WindowedPaginationMixin,
    ListView,
):
    """Справочник компетенций."""

    template_name = "competencies/index.html"
    model = Competency
    context_object_name = "competencies"
    paginate_by = 30
    page_title = _("Справочник компетенций")
    page_subtitle = _("Наблюдаемые характеристики поведения с индикаторами по уровням шкалы")
    filter_form_class = CompetencyFilterForm
    sort_fields = {
        "name": "name",
        "category": "category__sort_order",
        "kind": "kind",
        "usage": "requirement_count",
    }
    default_sort = ""

    def get_queryset(self):
        queryset = (
            Competency.objects.with_category()
            .annotate(
                scale_count=Count("scale_levels", distinct=True),
                requirement_count=Count("requirements", distinct=True),
            )
            .order_by("category__sort_order", "sort_order", "name")
        )
        # Выборка строится здесь целиком, а не через `FilterMixin`: отбор по полноте шкалы
        # опирается на аннотацию `scale_count`, и она должна появиться до применения фильтра, а не
        # после.
        form = self.get_filter_form()
        if form is not None and form.is_valid():
            queryset = form.filter(queryset)
        self._filter_form = form
        return self.apply_sort(queryset)

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["categories"] = CompetencyCategory.objects.filter(is_active=True).annotate(
            competency_count=Count("competencies", filter=Q(competencies__is_active=True))
        )
        context["expected_scale"] = Competency.expected_scale_size()
        context["total_count"] = Competency.objects.count()
        context["core_count"] = Competency.objects.filter(is_core=True).count()
        context["testable_count"] = Competency.objects.filter(is_testable=True).count()
        context["incomplete_count"] = (
            Competency.objects.annotate(levels=Count("scale_levels", distinct=True))
            .exclude(levels=Competency.expected_scale_size())
            .count()
        )
        return context


class CompetencyDetailView(CompetencyBaseMixin, DetailView):
    """Карточка компетенции: шкала, применение, распределение оценок."""

    template_name = "competencies/competency_detail.html"
    model = Competency
    context_object_name = "competency"

    def get_queryset(self):
        return Competency.objects.with_category().prefetch_related("scale_levels")

    def get_page_title(self) -> str:
        return self.object.name

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        competency = self.object

        context["page_subtitle"] = competency.definition
        context["scale"] = list(competency.scale_levels.all())
        context["requirements"] = (
            competency.requirements.select_related("competency_model")
            .filter(competency_model__is_active=True)
            .order_by("-weight")
        )
        context["tests"] = competency.tests.filter(is_active=True)

        # Распределение оценок по уровням: группировка одним запросом.
        distribution = (
            competency.scores.counted()
            .values("level")
            .annotate(count=Count("id"))
            .order_by("level")
        )
        rows = [{"level": float(item["level"]), "count": item["count"]} for item in distribution]
        total = sum(row["count"] for row in rows) or 1
        peak = max((row["count"] for row in rows), default=1) or 1
        for row in rows:
            row["share"] = round(row["count"] * 100 / total, 1)
            # Высота столбика считается от наибольшего значения, а не от суммы: при двадцати
            # уровнях доли малы, и диаграмма по долям выродилась бы в ровную полосу у основания.
            row["height"] = round(row["count"] * 100 / peak, 1)
        context["distribution"] = rows
        context["score_count"] = sum(row["count"] for row in rows)
        context["average_level"] = competency.scores.counted().aggregate(mean=Avg("level"))["mean"]
        return context


class CategoryDetailView(CompetencyBaseMixin, DetailView):
    """Категория справочника с входящими компетенциями."""

    template_name = "competencies/category_detail.html"
    model = CompetencyCategory
    context_object_name = "category"

    def get_page_title(self) -> str:
        return self.object.name

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_subtitle"] = self.object.description
        context["competencies"] = (
            self.object.competencies.filter(is_active=True)
            .annotate(
                scale_count=Count("scale_levels", distinct=True),
                requirement_count=Count("requirements", distinct=True),
            )
            .order_by("sort_order", "name")
        )
        context["expected_scale"] = Competency.expected_scale_size()
        return context


# ---------------------------------------------------------------------------
# Модели требований
# ---------------------------------------------------------------------------
class CompetencyModelListView(CompetencyBaseMixin, ListView):
    """Модели требований к должностям."""

    template_name = "competencies/models.html"
    model = CompetencyModel
    context_object_name = "models"
    page_title = _("Модели компетенций")
    page_subtitle = _("Наборы требований: целевой уровень и вес каждой компетенции")

    def get_queryset(self):
        return (
            CompetencyModel.objects.select_related("grade")
            .annotate(
                requirement_count=Count("requirements", distinct=True),
                position_count=Count("positions", distinct=True),
            )
            .order_by("-is_active", "name")
        )

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        models = list(context["models"])

        # Суммы весов читаются одним запросом и раскладываются по моделям.
        totals = dict(
            CompetencyRequirement.objects.filter(competency_model__in=models)
            .values("competency_model_id")
            .annotate(total=Sum("weight"))
            .values_list("competency_model_id", "total")
        )
        for item in models:
            item.weight_total = totals.get(item.pk) or Decimal("0")
            item.is_balanced = abs(item.weight_total - Decimal("1")) <= WEIGHT_SUM_TOLERANCE

        context["published_count"] = sum(1 for item in models if item.is_published)
        context["unbalanced"] = [item for item in models if item.is_active and not item.is_balanced]
        context["without_positions"] = [
            item for item in models if item.is_active and not item.position_count
        ]
        return context


class CompetencyModelDetailView(CompetencyBaseMixin, DetailView):
    """Модель требований: состав, веса, охваченные должности."""

    template_name = "competencies/model_detail.html"
    model = CompetencyModel
    context_object_name = "competency_model"

    def get_queryset(self):
        return CompetencyModel.objects.select_related("grade").prefetch_related(
            "requirements__competency__category", "positions__department"
        )

    def get_page_title(self) -> str:
        return self.object.name

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        competency_model = self.object
        requirements = list(competency_model.requirements.with_competency())

        context["page_subtitle"] = _("Версия %(version)s") % {"version": competency_model.version}
        context["requirements"] = requirements
        context["positions"] = competency_model.positions.select_related("grade", "department")
        context["weight_sum"] = competency_model.weight_sum
        context["critical_count"] = sum(1 for item in requirements if item.is_critical)

        # Свод по категориям: доля веса, приходящаяся на каждую группу компетенций.
        by_category: dict[int, dict] = {}
        for requirement in requirements:
            category = requirement.competency.category
            bucket = by_category.setdefault(
                category.pk, {"category": category, "weight": 0.0, "count": 0}
            )
            bucket["weight"] += float(requirement.weight)
            bucket["count"] += 1
        total = sum(bucket["weight"] for bucket in by_category.values()) or 1
        for bucket in by_category.values():
            bucket["share"] = round(bucket["weight"] * 100 / total, 1)
        context["category_shares"] = sorted(by_category.values(), key=lambda item: -item["weight"])

        context["assessed_employees"] = Employee.objects.filter(
            position__competency_models=competency_model
        ).count()
        return context


# ---------------------------------------------------------------------------
# Профиль соответствия
# ---------------------------------------------------------------------------
class ProfileContextMixin:
    """Сборка контекста страницы профиля компетенций."""

    def profile_context(self, employee: Employee, competency_model=None) -> dict[str, Any]:
        """Собирает профиль, диаграмму и разрезы по категориям."""
        profile = build_profile(employee, competency_model)
        return {
            "employee": employee,
            "profile": profile,
            "competency_model": profile.competency_model,
            "radar": build_radar(profile),
            "categories": profile.by_category(),
            "priorities": profile.development_priorities(5),
            "strengths": profile.strengths[:5],
            "blocking": profile.blocking_gaps,
            "unassessed": profile.unassessed,
            "window_months": DEFAULT_WINDOW_MONTHS,
        }


class MyCompetencyProfileView(CompetencyBaseMixin, ProfileContextMixin, TemplateView):
    """Профиль компетенций текущего пользователя."""

    template_name = "competencies/profile.html"
    page_title = _("Мои компетенции")
    page_subtitle = _("Соответствие требованиям должности и зоны развития")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        employee = getattr(self.request.user, "employee_card", None)

        if employee is None:
            context["employee"] = None
            context["profile"] = None
            return context

        context.update(self.profile_context(employee))
        context["is_own_profile"] = True
        return context


class EmployeeCompetencyProfileView(CompetencyBaseMixin, ProfileContextMixin, TemplateView):
    """Профиль компетенций сотрудника для руководителя и кадровой службы."""

    template_name = "competencies/profile.html"
    allowed_roles = PROFILE_ROLES
    page_title = _("Профиль компетенций")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        employee = get_object_or_404(
            Employee.objects.select_related("position", "department", "branch"),
            public_id=self.kwargs["public_id"],
        )

        if not can_view_employee(current_user(self.request), employee):
            raise PermissionDenied(_("Карточка сотрудника недоступна"))

        target_model = self._target_model()
        context.update(self.profile_context(employee, target_model))
        context["page_title"] = employee.full_name
        context["page_subtitle"] = employee.title
        context["is_own_profile"] = False
        context["own_model"] = model_for_employee(employee)
        context["target_model"] = target_model
        context["comparable_models"] = CompetencyModel.objects.published().order_by("name")
        return context

    def _target_model(self) -> CompetencyModel | None:
        """Модель, относительно которой строится профиль."""
        code = self.request.GET.get("model", "")
        if not code:
            return None
        return CompetencyModel.objects.published().filter(code=code).first()

    def get_breadcrumbs(self, trail):
        trail.add(_("Компетенции"), "competencies:index")
        trail.add(_("Профили"))
        return trail


# ---------------------------------------------------------------------------
# Матрица разрывов
# ---------------------------------------------------------------------------
class GapMatrixView(CompetencyBaseMixin, TemplateView):
    """Матрица разрывов подразделения."""

    template_name = "competencies/matrix.html"
    allowed_roles = MATRIX_ROLES
    page_title = _("Матрица разрывов")
    page_subtitle = _("Соответствие требованиям по подразделению: где проседает команда")

    #: Наибольшее число сотрудников в матрице.
    MAX_EMPLOYEES = 60

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        form = GapMatrixFilterForm(self.request.GET or None, user=self.request.user)
        context["filter_form"] = form
        context["departments"] = self._available_departments()

        if not form.is_valid():
            return context

        competency_model = form.cleaned_data.get("competency_model")
        department = form.cleaned_data.get("department")

        if competency_model is None:
            context["hint"] = _("Выберите модель требований, по которой сравнивать сотрудников")
            return context

        employees = self._employees(department, competency_model)
        if not employees:
            context["hint"] = _("В выбранном подразделении нет сотрудников с этой моделью")
            return context

        matrix = build_gap_matrix(employees, competency_model)
        context["matrix"] = matrix
        context["rows"] = matrix.rows()
        context["columns"] = matrix.columns()
        context["competency_model"] = competency_model
        context["department"] = department
        context["truncated"] = len(employees) >= self.MAX_EMPLOYEES
        return context

    def _available_departments(self):
        """Подразделения в границах области видимости пользователя."""
        queryset = Department.objects.filter(is_active=True).select_related("branch")
        user = current_user(self.request)
        if is_privileged(user):
            if user.scope_level == ScopeLevel.BRANCH:
                queryset = queryset.filter(branch_id=user.branch_id)
            return queryset.order_by("path")
        if user.department_id:
            subtree = Department.objects.descendant_ids(user.department_id)
            return queryset.filter(pk__in=subtree).order_by("path")
        return queryset.none()

    def _employees(self, department, competency_model) -> list[Employee]:
        """Сотрудники, попадающие в матрицу."""
        employees = (
            Employee.objects.active()
            .internal()
            .with_org()
            .filter(position__competency_models=competency_model)
        )

        if department is not None:
            employees = employees.in_department_tree(department.pk)

        from core.permissions import apply_employee_scope

        scoped: QuerySet[Employee] = apply_employee_scope(employees, current_user(self.request))
        return list(scoped.order_by("last_name", "first_name")[: self.MAX_EMPLOYEES])


# ---------------------------------------------------------------------------
# Публичный обзор шкалы — для страницы портала
# ---------------------------------------------------------------------------
def scale_levels_for(competency_ids: list[int]) -> dict[int, list[CompetencyScaleLevel]]:
    """Уровни шкалы нескольких компетенций одним запросом."""
    result: dict[int, list[CompetencyScaleLevel]] = {
        competency_id: [] for competency_id in competency_ids
    }
    for level in CompetencyScaleLevel.objects.filter(competency_id__in=competency_ids).order_by(
        "competency_id", "level"
    ):
        result[level.competency_id].append(level)
    return result
