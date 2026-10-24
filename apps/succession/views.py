"""Представления планирования преемственности."""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.generic import DetailView, ListView, TemplateView
from django.views.generic.edit import CreateView, UpdateView

from apps.succession.forms import (
    AppointmentFilterForm,
    AppointmentForm,
    KeyPositionFilterForm,
    KeyPositionForm,
    SlateForm,
    SlateOrderForm,
    SlateRemovalForm,
    SuccessionPlanFilterForm,
    SuccessionPlanForm,
)
from apps.succession.models import (
    Appointment,
    KeyPosition,
    PlanStatus,
    SuccessionPlan,
    SuccessionSlate,
)
from apps.succession.services import (
    SuccessionError,
    activate_plan,
    add_successor,
    appointment_statistics,
    close_plan,
    coverage_report,
    create_plan,
    key_positions,
    plans_due_for_review,
    register_appointment,
    remove_successor,
    reorder_slate,
    risk_matrix,
    suggest_successors,
    sync_key_positions,
    sync_slate_readiness,
    uncovered_positions,
)
from core.constants import Role
from core.mixins import (
    AuditedFormMixin,
    AuthenticatedRequest,
    BreadcrumbMixin,
    FilterMixin,
    PageMetaMixin,
    RoleRequiredMixin,
    WindowedPaginationMixin,
)

#: Роли, ведущие планирование преемственности.
PLANNER_ROLES: tuple[str, ...] = (Role.ADMIN, Role.HR)

#: Роли, которым доступен просмотр раздела.
VIEWER_ROLES: tuple[str, ...] = (Role.ADMIN, Role.HR, Role.MANAGER)


class SuccessionBaseMixin(RoleRequiredMixin, PageMetaMixin, BreadcrumbMixin):
    """Общее поведение страниц раздела преемственности."""

    allowed_roles = VIEWER_ROLES
    page_section = "succession"
    permission_denied_message = _("Раздел преемственности доступен кадровой службе")

    def get_breadcrumbs(self, trail):
        trail.add(_("Преемственность"), "succession:list")
        title = getattr(self, "page_title", "")
        if title and self.request.resolver_match.url_name != "list":
            trail.add(str(title))
        return trail

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_section_label"] = _("Преемственность")
        context["can_plan"] = self.request.user.role in PLANNER_ROLES
        return context


# ---------------------------------------------------------------------------
# Обзор
# ---------------------------------------------------------------------------
class SuccessionOverviewView(SuccessionBaseMixin, TemplateView):
    """Обзор преемственности: обеспеченность, матрица риска, срочные дела."""

    template_name = "succession/index.html"
    page_title = _("Преемственность")
    page_subtitle = _("Обеспеченность критичных должностей подготовленной заменой")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["report"] = coverage_report()
        context["matrix"] = risk_matrix()
        context["uncovered"] = uncovered_positions()[:10]
        context["due_plans"] = plans_due_for_review()[:10]
        context["statistics"] = appointment_statistics(months=12)
        context["recent_appointments"] = Appointment.objects.with_related().order_by(
            "-appointment_date"
        )[:8]
        return context


class CoverageView(SuccessionBaseMixin, TemplateView):
    """Обеспеченность в разрезе критичности."""

    template_name = "succession/coverage.html"
    page_title = _("Обеспеченность преемниками")
    page_subtitle = _("Покрытие, глубина скамейки и немедленная готовность по уровням критичности")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        report = coverage_report()
        context["report"] = report
        context["criticality_rows"] = sorted(
            report.by_criticality.items(), key=lambda item: -item[0]
        )
        context["statistics"] = appointment_statistics(months=24)
        return context


# ---------------------------------------------------------------------------
# Критичные должности
# ---------------------------------------------------------------------------
class KeyPositionListView(SuccessionBaseMixin, FilterMixin, WindowedPaginationMixin, ListView):
    """Перечень критичных должностей с обеспеченностью и приоритетом."""

    template_name = "succession/key_positions.html"
    model = KeyPosition
    context_object_name = "positions"
    paginate_by = 25
    page_title = _("Критичные должности")
    page_subtitle = _("Должности, уход с которых создаёт риск для деятельности организации")
    filter_form_class = KeyPositionFilterForm

    def get_queryset(self):
        queryset = key_positions()
        form = self.get_filter_form()
        if form is not None and form.is_valid():
            queryset = form.filter(queryset)
        self._filter_form = form
        return queryset

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["report"] = coverage_report()
        return context


class KeyPositionDetailView(SuccessionBaseMixin, DetailView):
    """Карточка критичной должности: риск, планы, предложенные преемники."""

    template_name = "succession/key_position_detail.html"
    model = KeyPosition
    context_object_name = "key_position"
    page_title = _("Критичная должность")

    def get_queryset(self):
        return KeyPosition.objects.with_related()

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        key_position: KeyPosition = self.object
        plans = (
            key_position.plans.select_related("owner")
            .annotate(
                successors=Count("slate", filter=Q(slate__removed_on__isnull=True), distinct=True)
            )
            .order_by("-created_at")
        )
        context["plans"] = plans
        context["active_plan"] = next(
            (plan for plan in plans if plan.status == PlanStatus.ACTIVE), None
        )
        context["suggestions"] = suggest_successors(
            key_position, exclude_plan=context["active_plan"]
        )
        context["page_title"] = key_position.position.name
        return context


class KeyPositionCreateView(SuccessionBaseMixin, AuditedFormMixin, CreateView):
    """Отнесение должности к критичным."""

    template_name = "succession/key_position_form.html"
    model = KeyPosition
    form_class = KeyPositionForm
    allowed_roles = PLANNER_ROLES
    page_title = _("Новая критичная должность")

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        form.instance.reviewed_at = timezone.localdate()
        response = super().form_valid(form)
        messages.success(self.request, _("Должность отнесена к критичным"))
        return response


class KeyPositionUpdateView(SuccessionBaseMixin, AuditedFormMixin, UpdateView):
    """Изменение характеристик критичной должности."""

    template_name = "succession/key_position_form.html"
    model = KeyPosition
    form_class = KeyPositionForm
    allowed_roles = PLANNER_ROLES
    page_title = _("Изменение критичной должности")

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        form.instance.reviewed_at = timezone.localdate()
        response = super().form_valid(form)
        messages.success(self.request, _("Изменения сохранены"))
        return response


@login_required
def sync_key_positions_view(request: AuthenticatedRequest) -> HttpResponse:
    """Приводит перечень критичных должностей в соответствие штатному расписанию."""
    if request.user.role not in PLANNER_ROLES:
        raise PermissionDenied(_("Действие доступно кадровой службе"))
    if request.method != "POST":
        return redirect("succession:key_positions")

    result = sync_key_positions(actor=request.user)
    messages.success(
        request,
        _("Сверка выполнена: добавлено %(created)s, выведено из планирования %(retired)s") % result,
    )
    return redirect("succession:key_positions")


# ---------------------------------------------------------------------------
# Планы преемственности
# ---------------------------------------------------------------------------
class SuccessionPlanListView(SuccessionBaseMixin, FilterMixin, WindowedPaginationMixin, ListView):
    """Планы преемственности организации."""

    template_name = "succession/plans.html"
    model = SuccessionPlan
    context_object_name = "plans"
    paginate_by = 25
    page_title = _("Планы преемственности")
    page_subtitle = _("Кем и в какие сроки закрывается каждая критичная должность")
    filter_form_class = SuccessionPlanFilterForm

    def get_queryset(self):
        queryset = (
            SuccessionPlan.objects.with_related()
            .annotate(
                successors_total=Count(
                    "slate", filter=Q(slate__removed_on__isnull=True), distinct=True
                )
            )
            .order_by("-created_at")
        )
        form = self.get_filter_form()
        if form is not None and form.is_valid():
            queryset = form.filter(queryset)
        self._filter_form = form
        return queryset


class SuccessionPlanDetailView(SuccessionBaseMixin, DetailView):
    """План преемственности: список преемников, риски, предложения к пополнению."""

    template_name = "succession/plan_detail.html"
    model = SuccessionPlan
    context_object_name = "plan"
    slug_field = "public_id"
    slug_url_kwarg = "public_id"
    page_title = _("План преемственности")

    def get_queryset(self):
        return SuccessionPlan.objects.with_related().select_related(
            "key_position__incumbent", "key_position__position__grade"
        )

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        plan: SuccessionPlan = self.object
        context["slate"] = (
            plan.slate.filter(removed_on__isnull=True)
            .select_related("employee", "employee__department", "entry", "entry__target_position")
            .order_by("rank")
        )
        context["removed"] = (
            plan.slate.filter(removed_on__isnull=False)
            .select_related("employee")
            .order_by("-removed_on")
        )
        context["suggestions"] = suggest_successors(plan.key_position, exclude_plan=plan)
        context["slate_form"] = SlateForm(plan=plan)
        context["appointments"] = plan.appointments.with_related().order_by("-appointment_date")
        context["page_title"] = plan.title
        return context


class SuccessionPlanUpdateView(SuccessionBaseMixin, AuditedFormMixin, UpdateView):
    """Изменение параметров плана."""

    template_name = "succession/plan_form.html"
    model = SuccessionPlan
    form_class = SuccessionPlanForm
    slug_field = "public_id"
    slug_url_kwarg = "public_id"
    allowed_roles = PLANNER_ROLES
    page_title = _("Изменение плана преемственности")

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, _("Изменения сохранены"))
        return response


@login_required
def create_plan_view(request: AuthenticatedRequest, pk: int) -> HttpResponse:
    """Создаёт план преемственности по критичной должности."""
    if request.user.role not in PLANNER_ROLES:
        raise PermissionDenied(_("Планирование доступно кадровой службе"))

    key_position = get_object_or_404(KeyPosition.objects.with_related(), pk=pk)
    if request.method != "POST":
        return redirect(key_position.get_absolute_url())

    try:
        plan = create_plan(key_position, actor=request.user)
    except SuccessionError as error:
        messages.error(request, str(error))
        return redirect(key_position.get_absolute_url())

    messages.success(request, _("План создан. Добавьте преемников и введите его в действие"))
    return redirect(plan.get_absolute_url())


@login_required
def plan_action_view(request: AuthenticatedRequest, public_id, action: str) -> HttpResponse:
    """Ввод плана в действие, закрытие и отмена."""
    if request.user.role not in PLANNER_ROLES:
        raise PermissionDenied(_("Действие доступно кадровой службе"))

    plan = get_object_or_404(SuccessionPlan.objects.with_related(), public_id=public_id)
    if request.method != "POST":
        return redirect(plan.get_absolute_url())

    reason = request.POST.get("reason", "")
    try:
        if action == "activate":
            activate_plan(plan, actor=request.user)
            messages.success(request, _("План введён в действие"))
        elif action == "close":
            close_plan(plan, actor=request.user, reason=reason)
            messages.success(request, _("План закрыт"))
        elif action == "cancel":
            close_plan(plan, actor=request.user, reason=reason, cancel=True)
            messages.success(request, _("План отменён"))
        elif action == "sync":
            updated = sync_slate_readiness(plan)
            messages.success(
                request,
                _("Готовность преемников обновлена: изменено записей — %(count)s")
                % {"count": updated},
            )
        else:  # pragma: no cover - защита от неизвестного действия в адресе
            raise PermissionDenied(_("Неизвестное действие"))
    except SuccessionError as error:
        messages.error(request, str(error))

    return redirect(plan.get_absolute_url())


# ---------------------------------------------------------------------------
# Список преемников
# ---------------------------------------------------------------------------
@login_required
def add_successor_view(request: AuthenticatedRequest, public_id) -> HttpResponse:
    """Включает сотрудника в список преемников плана."""
    if request.user.role not in PLANNER_ROLES:
        raise PermissionDenied(_("Действие доступно кадровой службе"))

    plan = get_object_or_404(SuccessionPlan.objects.with_related(), public_id=public_id)
    if request.method != "POST":
        return redirect(plan.get_absolute_url())

    form = SlateForm(request.POST, plan=plan)
    if not form.is_valid():
        for errors in form.errors.values():
            messages.error(request, "; ".join(str(item) for item in errors))
        return redirect(plan.get_absolute_url())

    try:
        slate = add_successor(
            plan,
            form.cleaned_data["employee"],
            is_emergency=form.cleaned_data["is_emergency_successor"],
            development_focus=form.cleaned_data["development_focus"],
        )
    except SuccessionError as error:
        messages.error(request, str(error))
    else:
        messages.success(
            request,
            _("%(name)s включён в список преемников под рангом %(rank)s")
            % {"name": slate.employee.short_name, "rank": slate.rank},
        )
    return redirect(plan.get_absolute_url())


@login_required
def remove_successor_view(request: AuthenticatedRequest, pk: int) -> HttpResponse:
    """Исключает преемника из плана с указанием основания."""
    if request.user.role not in PLANNER_ROLES:
        raise PermissionDenied(_("Действие доступно кадровой службе"))

    slate = get_object_or_404(SuccessionSlate.objects.select_related("plan", "employee"), pk=pk)
    form = SlateRemovalForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        try:
            remove_successor(slate, reason=form.cleaned_data["reason"], actor=request.user)
        except SuccessionError as error:
            messages.error(request, str(error))
        else:
            messages.success(request, _("Преемник исключён из плана"))
        return redirect(slate.plan.get_absolute_url())

    return render(
        request,
        "succession/slate_remove.html",
        {
            "slate": slate,
            "form": form,
            "page_title": _("Исключение преемника"),
            "page_section_label": _("Преемственность"),
        },
    )


@login_required
def reorder_slate_view(request: AuthenticatedRequest, public_id) -> HttpResponse:
    """Меняет порядок преемников в плане."""
    if request.user.role not in PLANNER_ROLES:
        raise PermissionDenied(_("Действие доступно кадровой службе"))

    plan = get_object_or_404(SuccessionPlan.objects.with_related(), public_id=public_id)
    if request.method != "POST":
        return redirect(plan.get_absolute_url())

    form = SlateOrderForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Порядок преемников не распознан"))
        return redirect(plan.get_absolute_url())

    try:
        reorder_slate(plan, form.cleaned_data["order"])
    except SuccessionError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, _("Порядок преемников сохранён"))
    return redirect(plan.get_absolute_url())


# ---------------------------------------------------------------------------
# Назначения
# ---------------------------------------------------------------------------
class AppointmentListView(SuccessionBaseMixin, FilterMixin, WindowedPaginationMixin, ListView):
    """Назначения — итог работы всей системы."""

    template_name = "succession/appointments.html"
    model = Appointment
    context_object_name = "appointments"
    paginate_by = 25
    page_title = _("Назначения")
    page_subtitle = _("Кем закрыты должности и какая доля назначений сделана из резерва")
    filter_form_class = AppointmentFilterForm

    def get_queryset(self):
        queryset = Appointment.objects.with_related().order_by("-appointment_date")
        form = self.get_filter_form()
        if form is not None and form.is_valid():
            queryset = form.filter(queryset)
        self._filter_form = form
        return queryset

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["statistics"] = appointment_statistics(year=timezone.localdate().year)
        context["statistics_all"] = appointment_statistics()
        return context


class AppointmentDetailView(SuccessionBaseMixin, DetailView):
    """Карточка назначения: источник, срок подготовки, предшественник."""

    template_name = "succession/appointment_detail.html"
    model = Appointment
    context_object_name = "appointment"
    slug_field = "public_id"
    slug_url_kwarg = "public_id"
    page_title = _("Назначение")

    def get_queryset(self):
        return Appointment.objects.with_related().select_related(
            "predecessor", "entry", "entry__target_position", "plan"
        )


class AppointmentCreateView(SuccessionBaseMixin, TemplateView):
    """Регистрация назначения."""

    template_name = "succession/appointment_form.html"
    allowed_roles = PLANNER_ROLES
    page_title = _("Регистрация назначения")
    page_subtitle = _("Назначение закрывает цикл: выдвижение — резерв — развитие — должность")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        initial = {
            key: value for key, value in self.request.GET.items() if key in {"employee", "position"}
        }
        context.setdefault("form", AppointmentForm(initial=initial, user=self.request.user))
        return context

    def post(self, request: AuthenticatedRequest, *args, **kwargs) -> HttpResponse:
        form = AppointmentForm(request.POST, user=request.user)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))

        data = form.cleaned_data
        appointment = register_appointment(
            employee=data["employee"],
            position=data["position"],
            appointment_date=data["appointment_date"],
            kind=data["kind"],
            actor=request.user,
            order_number=data["order_number"],
            comment=data["comment"],
        )
        if data.get("probation_until"):
            appointment.probation_until = data["probation_until"]
            appointment.save(update_fields=["probation_until", "updated_at"])

        messages.success(
            request,
            _("Назначение зарегистрировано: %(name)s → %(position)s")
            % {
                "name": appointment.employee.short_name,
                "position": appointment.position.name,
            },
        )
        return redirect(appointment.get_absolute_url())
