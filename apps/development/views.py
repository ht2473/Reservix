"""Представления раздела развития."""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Prefetch, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.generic import DetailView, ListView, TemplateView
from django.views.generic.edit import CreateView, UpdateView

from apps.development.forms import (
    DEFAULT_EVIDENCE_KIND,
    ActivityCompletionForm,
    ActivityFilterForm,
    DevelopmentActivityForm,
    DevelopmentPlanForm,
    EvidenceForm,
    MeetingForm,
    MentorshipCompletionForm,
    MentorshipForm,
    PlanCompletionForm,
    PlanFilterForm,
    ResourceFilterForm,
)
from apps.development.models import (
    ActivityStatus,
    DevelopmentActivity,
    DevelopmentPlan,
    LearningResource,
    Mentorship,
    MentorshipStatus,
    PlanStatus,
)
from apps.development.services import (
    DevelopmentError,
    add_evidence,
    approve_plan,
    cancel_plan,
    complete_activity,
    complete_mentorship,
    complete_plan,
    development_summary,
    fill_plan_from_gaps,
    library_statistics,
    log_meeting,
    mentor_load,
    mentorships_for,
    overdue_activities,
    plans_for,
    start_mentorship,
    submit_plan,
    suggest_activities,
    upcoming_activities,
    verify_evidence,
)
from apps.reserve.services import can_open_entry
from core.breadcrumbs import BreadcrumbTrail
from core.constants import PRIVILEGED_ROLES, Role
from core.mixins import (
    AuditedFormMixin,
    AuthenticatedRequest,
    BreadcrumbMixin,
    FilterMixin,
    PageMetaMixin,
    RoleRequiredMixin,
    WindowedPaginationMixin,
)
from core.permissions import can_view_employee, current_user

#: Роли, ведущие планы развития: создают, наполняют и закрывают.
PLANNER_ROLES: tuple[str, ...] = (Role.ADMIN, Role.HR)

#: Роли, которым доступен раздел целиком.
VIEWER_ROLES: tuple[str, ...] = (Role.ADMIN, Role.HR, Role.MANAGER, Role.MENTOR)


class DevelopmentSectionMixin(RoleRequiredMixin, PageMetaMixin, BreadcrumbMixin):
    """Общее поведение страниц раздела развития."""

    allowed_roles = VIEWER_ROLES
    page_section = "development"
    permission_denied_message = _("Раздел развития доступен кадровой службе и руководителям")

    def get_breadcrumbs(self, trail: BreadcrumbTrail) -> BreadcrumbTrail:
        # Корень цепочки — обзорная страница раздела, доступная этой роли.
        root = (
            "development:list" if self.request.user.role in VIEWER_ROLES else "development:my_plan"
        )
        trail.add(_("Развитие"), root)
        title = getattr(self, "page_title", "")
        if title and self.request.resolver_match.url_name != "list":
            trail.add(str(title))
        return trail

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_section_label"] = _("Развитие")
        context["can_plan"] = self.request.user.role in PLANNER_ROLES
        return context


# ---------------------------------------------------------------------------
# Планы развития
# ---------------------------------------------------------------------------
class PlanListView(DevelopmentSectionMixin, FilterMixin, WindowedPaginationMixin, ListView):
    """Перечень планов развития с показателями хода выполнения."""

    template_name = "development/index.html"
    model = DevelopmentPlan
    context_object_name = "plans"
    paginate_by = 25
    filter_form_class = PlanFilterForm
    page_title = _("Планы развития")
    page_subtitle = _("Индивидуальные планы, ход выполнения и просроченные мероприятия")

    def get_queryset(self):
        today = timezone.localdate()
        queryset = plans_for(current_user(self.request)).annotate(
            activities_total=Count("activities", distinct=True),
            activities_done=Count(
                "activities",
                filter=Q(activities__status=ActivityStatus.COMPLETED),
                distinct=True,
            ),
            activities_overdue=Count(
                "activities",
                filter=Q(
                    activities__due_date__lt=today,
                    activities__status__in=[ActivityStatus.PENDING, ActivityStatus.IN_PROGRESS],
                ),
                distinct=True,
            ),
        )

        form = self.get_filter_form()
        if form is not None and form.is_valid():
            queryset = form.filter(queryset)
        self._filter_form = form
        return queryset.order_by("-start_date")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["summary"] = development_summary(user=current_user(self.request))
        context["overdue"] = overdue_activities(user=current_user(self.request))[:8]
        context["upcoming"] = upcoming_activities(user=current_user(self.request))[:8]
        return context


class PlanDetailView(DevelopmentSectionMixin, DetailView):
    """Карточка плана развития."""

    template_name = "development/plan_detail.html"
    model = DevelopmentPlan
    context_object_name = "plan"
    slug_field = "public_id"
    slug_url_kwarg = "public_id"
    # Сотрудник допущен к карточке собственного плана: на неё ссылается карточка мероприятия, а
    # состав доступных планов ограничен ниже, в `get_queryset`, — для сотрудника это его
    # единственный план.
    allowed_roles = (*VIEWER_ROLES, Role.EMPLOYEE)
    page_title = _("План развития")

    def get_queryset(self):
        return plans_for(current_user(self.request)).prefetch_related(
            Prefetch(
                "activities",
                queryset=DevelopmentActivity.objects.with_related()
                .annotate(evidence_total=Count("evidences"))
                .order_by("due_date"),
            )
        )

    def get_page_title(self) -> str:
        return self.object.title

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        plan: DevelopmentPlan = self.object
        activities = list(plan.activities.all())

        context["activities"] = activities
        context["today"] = timezone.localdate()
        context["mentorships"] = plan.mentorships.select_related("mentor", "mentee")
        # Ссылка на пару показывается тому, кому карточка пары доступна.
        context["open_pairs"] = set(
            mentorships_for(current_user(self.request))
            .filter(plan=plan)
            .values_list("pk", flat=True)
        )
        # Право открыть запись резерва не следует из права видеть план: раздел резерва закрыт для
        # сотрудника и наставника целиком, а наставник к тому же ведёт подопечного за пределами
        # своей области видимости.
        context["can_open_entry"] = can_open_entry(current_user(self.request), plan.entry)
        context["completion_form"] = PlanCompletionForm()
        context["can_manage"] = (
            self.request.user.role in PLANNER_ROLES or plan.supervisor_id == self.request.user.pk
        )
        context["suggestions"] = self._suggestions(plan)
        return context

    def _suggestions(self, plan: DevelopmentPlan) -> list:
        """Предложения мероприятий по незакрытым разрывам."""
        if plan.status != PlanStatus.DRAFT:
            return []

        from apps.competencies.services import build_profile

        competency_model = None
        entry = plan.entry
        if entry is not None:
            from apps.reserve.services import target_profile

            target = target_profile(entry)
            if target is not None:
                competency_model = target.competency_model

        profile = build_profile(plan.employee, competency_model)
        covered = set(plan.activities.values_list("competency_id", flat=True))
        return [
            suggestion
            for suggestion in suggest_activities(profile)
            if suggestion.competency_id not in covered
        ]


class PlanCreateView(DevelopmentSectionMixin, AuditedFormMixin, CreateView):
    """Создание плана развития."""

    template_name = "development/plan_form.html"
    model = DevelopmentPlan
    form_class = DevelopmentPlanForm
    allowed_roles = PLANNER_ROLES
    page_title = _("Новый план развития")
    page_subtitle = _("План строится от разрывов компетенций, а не от каталога курсов")

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_initial(self) -> dict[str, Any]:
        from core.utils import shift_months

        initial = super().get_initial()
        today = timezone.localdate()
        initial.setdefault("start_date", today)
        initial.setdefault("end_date", shift_months(today, 12))

        employee = self.request.GET.get("employee")
        if employee:
            initial["employee"] = employee
        return initial

    def form_valid(self, form):
        form.instance.status = PlanStatus.DRAFT
        response = super().form_valid(form)
        messages.success(
            self.request,
            _("План создан. Наполните его мероприятиями и отправьте на утверждение."),
        )
        return response


class PlanUpdateView(DevelopmentSectionMixin, AuditedFormMixin, UpdateView):
    """Правка плана развития."""

    template_name = "development/plan_form.html"
    model = DevelopmentPlan
    form_class = DevelopmentPlanForm
    slug_field = "public_id"
    slug_url_kwarg = "public_id"
    allowed_roles = PLANNER_ROLES
    page_title = _("Правка плана развития")

    def get_queryset(self):
        return plans_for(current_user(self.request))

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        if self.object.status in {PlanStatus.COMPLETED, PlanStatus.CANCELLED}:
            messages.error(self.request, _("Закрытый план не изменяется"))
            return redirect(self.object.get_absolute_url())
        messages.success(self.request, _("План сохранён"))
        return super().form_valid(form)


@login_required
def plan_action_view(request: AuthenticatedRequest, public_id, action: str) -> HttpResponse:
    """Действия над планом: наполнение, отправка на утверждение, утверждение, отмена."""
    plan = get_object_or_404(plans_for(request.user), public_id=public_id)

    is_supervisor = plan.supervisor_id == request.user.pk
    if request.user.role not in PLANNER_ROLES and not is_supervisor:
        raise PermissionDenied(_("Действие доступно кадровой службе и куратору плана"))

    if request.method != "POST":
        return redirect(plan.get_absolute_url())

    try:
        if action == "fill":
            created = fill_plan_from_gaps(plan, actor=request.user)
            if created:
                messages.success(
                    request,
                    _("Добавлено мероприятий по разрывам: %(count)s") % {"count": created},
                )
            else:
                messages.info(
                    request,
                    _("Новых разрывов не найдено: профиль компетенций закрыт либо не оценён"),
                )
        elif action == "submit":
            submit_plan(plan, actor=request.user)
            messages.success(request, _("План отправлен на утверждение"))
        elif action == "approve":
            approve_plan(plan, actor=request.user)
            messages.success(request, _("План утверждён и введён в действие"))
        elif action == "cancel":
            cancel_plan(plan, actor=request.user, reason=request.POST.get("reason", ""))
            messages.success(request, _("План отменён"))
        else:  # pragma: no cover - защита от неизвестного действия в адресе
            raise PermissionDenied(_("Неизвестное действие"))
    except DevelopmentError as error:
        messages.error(request, str(error))

    return redirect(plan.get_absolute_url())


@login_required
def plan_complete_view(request: AuthenticatedRequest, public_id) -> HttpResponse:
    """Завершение плана с подведением итога."""
    plan = get_object_or_404(plans_for(request.user), public_id=public_id)

    is_supervisor = plan.supervisor_id == request.user.pk
    if request.user.role not in PLANNER_ROLES and not is_supervisor:
        raise PermissionDenied(_("Завершение плана доступно кадровой службе и куратору"))

    form = PlanCompletionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            complete_plan(plan, actor=request.user, summary=form.cleaned_data["summary"])
        except DevelopmentError as error:
            messages.error(request, str(error))
        else:
            messages.success(request, _("План завершён, итог сохранён"))
            return redirect(plan.get_absolute_url())

    return render(
        request,
        "development/plan_complete.html",
        {
            "plan": plan,
            "form": form,
            "page_title": _("Завершение плана развития"),
            "page_section_label": _("Развитие"),
        },
    )


# ---------------------------------------------------------------------------
# Мероприятия
# ---------------------------------------------------------------------------
class ActivityBoardView(DevelopmentSectionMixin, FilterMixin, WindowedPaginationMixin, ListView):
    """Сводный перечень мероприятий развития."""

    template_name = "development/activities.html"
    model = DevelopmentActivity
    context_object_name = "activities"
    paginate_by = 40
    filter_form_class = ActivityFilterForm
    page_title = _("Мероприятия развития")
    page_subtitle = _("Сроки, ответственные и подтверждения выполнения")

    def get_queryset(self):
        queryset = DevelopmentActivity.objects.with_related().annotate(
            evidence_total=Count("evidences")
        )
        # Отбор по перечню доступных планов, а не по области видимости напрямую: карточка
        # мероприятия проверяет доступ именно так, и два разных правила расходились.
        user = current_user(self.request)
        queryset = queryset.filter(plan__in=plans_for(user).values("pk"))

        form = self.get_filter_form()
        if form is not None and form.is_valid():
            queryset = form.filter(queryset)
        self._filter_form = form
        return queryset.order_by("due_date")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["summary"] = development_summary(user=current_user(self.request))
        context["today"] = timezone.localdate()
        return context


class ActivityDetailView(DevelopmentSectionMixin, DetailView):
    """Карточка мероприятия: срок, ресурс, подтверждения."""

    template_name = "development/activity_detail.html"
    model = DevelopmentActivity
    context_object_name = "activity"
    slug_field = "public_id"
    slug_url_kwarg = "public_id"
    allowed_roles = (*VIEWER_ROLES, Role.EMPLOYEE)
    page_title = _("Мероприятие развития")

    def get_queryset(self):
        return DevelopmentActivity.objects.with_related().select_related(
            "plan__employee", "resource", "competency"
        )

    def get_object(self, queryset=None) -> DevelopmentActivity:
        activity = super().get_object(queryset)
        if not plans_for(current_user(self.request)).filter(pk=activity.plan_id).exists():
            raise PermissionDenied(_("Мероприятие относится к недоступному плану"))
        return activity

    def get_page_title(self) -> str:
        return self.object.title

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        activity: DevelopmentActivity = self.object
        context["evidences"] = activity.evidences.select_related("submitted_by", "verified_by")
        context["evidence_form"] = EvidenceForm(
            initial={"kind": DEFAULT_EVIDENCE_KIND.get(activity.activity_type)}
        )
        context["completion_form"] = ActivityCompletionForm()
        context["can_complete"] = activity.status not in {
            ActivityStatus.COMPLETED,
            ActivityStatus.CANCELLED,
        }
        return context


class ActivityCreateView(DevelopmentSectionMixin, CreateView):
    """Добавление мероприятия в план."""

    template_name = "development/activity_form.html"
    model = DevelopmentActivity
    form_class = DevelopmentActivityForm
    allowed_roles = (Role.ADMIN, Role.HR, Role.MANAGER)
    page_title = _("Новое мероприятие")

    def dispatch(self, request, *args, **kwargs):
        self.plan = get_object_or_404(
            DevelopmentPlan.objects.with_related(), public_id=kwargs["public_id"]
        )
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        kwargs["plan"] = self.plan
        return kwargs

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["plan"] = self.plan
        return context

    def form_valid(self, form):
        form.instance.plan = self.plan
        response = super().form_valid(form)
        self.plan.recalculate_completion()
        messages.success(self.request, _("Мероприятие добавлено в план"))
        return response

    def get_success_url(self) -> str:
        return self.plan.get_absolute_url()


class ActivityUpdateView(DevelopmentSectionMixin, UpdateView):
    """Правка мероприятия."""

    template_name = "development/activity_form.html"
    model = DevelopmentActivity
    form_class = DevelopmentActivityForm
    slug_field = "public_id"
    slug_url_kwarg = "public_id"
    allowed_roles = (Role.ADMIN, Role.HR, Role.MANAGER)
    page_title = _("Правка мероприятия")

    def get_queryset(self):
        return DevelopmentActivity.objects.with_related()

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        kwargs["plan"] = self.object.plan
        return kwargs

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["plan"] = self.object.plan
        return context

    def form_valid(self, form):
        response = super().form_valid(form)
        self.object.plan.recalculate_completion()
        messages.success(self.request, _("Мероприятие сохранено"))
        return response


@login_required
def activity_complete_view(request: AuthenticatedRequest, public_id) -> HttpResponse:
    """Отметка о выполнении мероприятия."""
    activity = get_object_or_404(
        DevelopmentActivity.objects.with_related().select_related("plan__employee"),
        public_id=public_id,
    )

    if not _may_report(request.user, activity):
        raise PermissionDenied(_("Отметить выполнение может исполнитель, куратор или наставник"))

    form = ActivityCompletionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            complete_activity(
                activity,
                actor=request.user,
                result=form.cleaned_data["result"],
                on_date=form.cleaned_data["completion_date"],
            )
        except DevelopmentError as error:
            messages.error(request, str(error))
        else:
            messages.success(request, _("Мероприятие отмечено выполненным"))
        return redirect(activity.get_absolute_url())

    return redirect(activity.get_absolute_url())


def _may_report(user, activity: DevelopmentActivity) -> bool:
    """Вправе ли пользователь отчитываться по мероприятию."""
    if user.role in PRIVILEGED_ROLES:
        return can_view_employee(user, activity.plan.employee)
    if activity.plan.supervisor_id == user.pk or activity.mentor_id == user.pk:
        return True
    # Ответственный хранится учётной записью, а не кадровой карточкой:
    # отчитывается о мероприятии тот, кто входит в систему.
    return activity.responsible_id == user.pk


@login_required
def evidence_add_view(request: AuthenticatedRequest, public_id) -> HttpResponse:
    """Прикладывает подтверждение выполнения мероприятия."""
    activity = get_object_or_404(
        DevelopmentActivity.objects.select_related("plan__employee"), public_id=public_id
    )

    if not _may_report(request.user, activity):
        raise PermissionDenied(_("Приложить подтверждение может исполнитель или куратор"))

    form = EvidenceForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            add_evidence(
                activity,
                actor=request.user,
                kind=form.cleaned_data["kind"],
                title=form.cleaned_data["title"],
                file=form.cleaned_data.get("file"),
                external_url=form.cleaned_data.get("external_url", ""),
                comment=form.cleaned_data.get("comment", ""),
            )
        except DevelopmentError as error:
            messages.error(request, str(error))
        else:
            messages.success(request, _("Подтверждение приложено"))
    elif request.method == "POST":
        for errors in form.errors.values():
            messages.error(request, "; ".join(str(item) for item in errors))

    return redirect(activity.get_absolute_url())


@login_required
def evidence_verify_view(request: AuthenticatedRequest, pk: int) -> HttpResponse:
    """Отмечает подтверждение проверенным."""
    from apps.development.models import ActivityEvidence

    evidence = get_object_or_404(ActivityEvidence.objects.select_related("activity__plan"), pk=pk)

    if request.method != "POST":
        return redirect(evidence.activity.get_absolute_url())

    try:
        verify_evidence(evidence, actor=request.user)
    except DevelopmentError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, _("Подтверждение проверено"))

    return redirect(evidence.activity.get_absolute_url())


# ---------------------------------------------------------------------------
# Личный план сотрудника
# ---------------------------------------------------------------------------
class MyPlanView(PageMetaMixin, BreadcrumbMixin, TemplateView):
    """Личный план развития сотрудника."""

    template_name = "development/my_plan.html"
    page_section = "development"
    page_title = _("Мой план развития")
    page_subtitle = _("Мероприятия, сроки и подтверждения выполнения")

    def get_breadcrumbs(self, trail: BreadcrumbTrail) -> BreadcrumbTrail:
        return trail.add(_("Мой план развития"))

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        employee = getattr(self.request.user, "employee", None)
        context["employee"] = employee

        if employee is None:
            context["plan"] = None
            return context

        plan = (
            DevelopmentPlan.objects.for_employee(employee.pk)
            .with_related()
            .order_by("-start_date")
            .first()
        )
        context["plan"] = plan
        context["today"] = timezone.localdate()

        if plan is not None:
            context["activities"] = (
                plan.activities.with_related()
                .annotate(evidence_total=Count("evidences"))
                .order_by("due_date")
            )
            context["mentorships"] = plan.mentorships.select_related("mentor")

        context["history"] = (
            DevelopmentPlan.objects.for_employee(employee.pk)
            .exclude(pk=plan.pk if plan else 0)
            .order_by("-start_date")[:5]
        )
        return context


# ---------------------------------------------------------------------------
# Наставничество
# ---------------------------------------------------------------------------
class MentorshipListView(DevelopmentSectionMixin, FilterMixin, WindowedPaginationMixin, ListView):
    """Пары наставничества."""

    template_name = "development/mentorship.html"
    model = Mentorship
    context_object_name = "pairs"
    paginate_by = 25
    page_title = _("Наставничество")
    page_subtitle = _("Пары, ход сопровождения и нагрузка наставников")

    def get_queryset(self):
        return mentorships_for(current_user(self.request)).order_by("-start_date")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["load"] = mentor_load()
        context["today"] = timezone.localdate()
        return context


class MentorshipDetailView(DevelopmentSectionMixin, DetailView):
    """Карточка наставнической пары: цель, встречи, результат."""

    template_name = "development/mentorship_detail.html"
    model = Mentorship
    context_object_name = "pair"
    slug_field = "public_id"
    slug_url_kwarg = "public_id"
    # Подопечный допущен к карточке собственной пары: на неё ссылается его план развития.
    allowed_roles = (*VIEWER_ROLES, Role.EMPLOYEE)
    page_title = _("Наставничество")

    def get_queryset(self):
        return mentorships_for(current_user(self.request))

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        pair: Mentorship = self.object
        context["meeting_form"] = MeetingForm()
        context["completion_form"] = MentorshipCompletionForm()
        context["can_manage"] = (
            self.request.user.role in PLANNER_ROLES or pair.mentor_id == self.request.user.pk
        )
        return context


class MentorshipCreateView(DevelopmentSectionMixin, CreateView):
    """Создание наставнической пары."""

    template_name = "development/mentorship_form.html"
    model = Mentorship
    form_class = MentorshipForm
    allowed_roles = PLANNER_ROLES
    page_title = _("Новая пара наставничества")
    page_subtitle = _("Наставник, подопечный, цель и ритм встреч")

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        data = form.cleaned_data
        try:
            pair = start_mentorship(
                mentor=data["mentor"],
                mentee=data["mentee"],
                goal=data["goal"],
                actor=current_user(self.request),
                plan=data.get("plan"),
                end_date=data.get("end_date"),
                frequency_days=data.get("meeting_frequency_days") or 14,
            )
        except DevelopmentError as error:
            form.add_error(None, str(error))
            return self.form_invalid(form)

        messages.success(self.request, _("Пара наставничества создана"))
        return redirect(pair.get_absolute_url())


@login_required
def mentorship_meeting_view(request: AuthenticatedRequest, public_id) -> HttpResponse:
    """Отмечает проведённую встречу."""
    pair = get_object_or_404(mentorships_for(request.user), public_id=public_id)

    if request.user.role not in PLANNER_ROLES and pair.mentor_id != request.user.pk:
        raise PermissionDenied(_("Встречи отмечает наставник или кадровая служба"))

    form = MeetingForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            log_meeting(
                pair,
                actor=request.user,
                on_date=form.cleaned_data["meeting_date"],
                note=form.cleaned_data.get("note", ""),
            )
        except DevelopmentError as error:
            messages.error(request, str(error))
        else:
            messages.success(request, _("Встреча отмечена"))
    elif request.method == "POST":
        messages.error(request, _("Дата встречи не распознана"))

    return redirect(pair.get_absolute_url())


@login_required
def mentorship_complete_view(request: AuthenticatedRequest, public_id) -> HttpResponse:
    """Завершает наставничество с фиксацией результата."""
    pair = get_object_or_404(mentorships_for(request.user), public_id=public_id)

    if request.user.role not in PLANNER_ROLES and pair.mentor_id != request.user.pk:
        raise PermissionDenied(_("Завершение пары доступно наставнику и кадровой службе"))

    form = MentorshipCompletionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            complete_mentorship(
                pair,
                actor=request.user,
                result=form.cleaned_data["result"],
                effectiveness=form.cleaned_data.get("effectiveness") or None,
            )
        except DevelopmentError as error:
            messages.error(request, str(error))
        else:
            messages.success(request, _("Наставничество завершено"))
    elif request.method == "POST":
        for errors in form.errors.values():
            messages.error(request, "; ".join(str(item) for item in errors))

    return redirect(pair.get_absolute_url())


# ---------------------------------------------------------------------------
# Рабочее место наставника
# ---------------------------------------------------------------------------
class MentorWorkspaceMixin(RoleRequiredMixin, PageMetaMixin, BreadcrumbMixin):
    """Общее поведение страниц наставника."""

    allowed_roles = (Role.MENTOR, Role.ADMIN, Role.HR)
    page_section = "development"
    permission_denied_message = _("Раздел доступен наставникам")

    def get_breadcrumbs(self, trail: BreadcrumbTrail) -> BreadcrumbTrail:
        trail.add(_("Наставничество"), "development:mentees")
        title = getattr(self, "page_title", "")
        if title and self.request.resolver_match.url_name != "mentees":
            trail.add(str(title))
        return trail

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_section_label"] = _("Наставничество")
        return context


class MenteeListView(MentorWorkspaceMixin, ListView):
    """Подопечные наставника."""

    template_name = "development/mentees.html"
    model = Mentorship
    context_object_name = "pairs"
    page_title = _("Мои подопечные")
    page_subtitle = _("Закреплённые сотрудники, ход сопровождения и срок встречи")

    def get_queryset(self):
        return (
            Mentorship.objects.with_related()
            .for_mentor(current_user(self.request).pk)
            .order_by("status", "-start_date")
        )

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        pairs = context["pairs"]
        context["due"] = [pair for pair in pairs if pair.is_meeting_due]
        context["active_count"] = sum(1 for pair in pairs if pair.status == MentorshipStatus.ACTIVE)
        return context


class MenteePlansView(MentorWorkspaceMixin, ListView):
    """Планы развития подопечных с ходом выполнения."""

    template_name = "development/mentee_plans.html"
    model = DevelopmentPlan
    context_object_name = "plans"
    page_title = _("Планы подопечных")
    page_subtitle = _("Выполнение мероприятий закреплёнными сотрудниками")

    def get_queryset(self):
        today = timezone.localdate()
        return (
            DevelopmentPlan.objects.with_related()
            .filter(
                employee__mentorships__mentor=self.request.user,
                employee__mentorships__status=MentorshipStatus.ACTIVE,
            )
            .annotate(
                activities_total=Count("activities", distinct=True),
                activities_done=Count(
                    "activities",
                    filter=Q(activities__status=ActivityStatus.COMPLETED),
                    distinct=True,
                ),
                activities_overdue=Count(
                    "activities",
                    filter=Q(
                        activities__due_date__lt=today,
                        activities__status__in=[
                            ActivityStatus.PENDING,
                            ActivityStatus.IN_PROGRESS,
                        ],
                    ),
                    distinct=True,
                ),
            )
            .distinct()
            .order_by("-start_date")
        )


class MentorFeedbackView(MentorWorkspaceMixin, ListView):
    """Подтверждения, ожидающие проверки наставником."""

    template_name = "development/mentor_feedback.html"
    context_object_name = "evidences"
    page_title = _("Обратная связь")
    page_subtitle = _("Подтверждения выполнения, ожидающие проверки")

    def get_queryset(self):
        from apps.development.models import ActivityEvidence

        return (
            ActivityEvidence.objects.filter(
                is_verified=False,
                activity__plan__employee__mentorships__mentor=self.request.user,
                activity__plan__employee__mentorships__status=MentorshipStatus.ACTIVE,
            )
            .select_related("activity__plan__employee", "submitted_by")
            .distinct()
            .order_by("-created_at")
        )

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["pairs"] = (
            Mentorship.objects.with_related().for_mentor(current_user(self.request).pk).active()
        )
        return context


# ---------------------------------------------------------------------------
# Библиотека развития
# ---------------------------------------------------------------------------
class ResourceListView(
    PageMetaMixin, BreadcrumbMixin, FilterMixin, WindowedPaginationMixin, ListView
):
    """Библиотека развития."""

    template_name = "development/resources.html"
    model = LearningResource
    context_object_name = "resources"
    paginate_by = 24
    filter_form_class = ResourceFilterForm
    page_section = "development"
    page_title = _("Библиотека развития")
    page_subtitle = _("Курсы, программы и материалы, привязанные к компетенциям")

    def get_breadcrumbs(self, trail: BreadcrumbTrail) -> BreadcrumbTrail:
        return trail.add(_("Библиотека развития"))

    def get_queryset(self):
        queryset = LearningResource.objects.filter(is_active=True).prefetch_related("competencies")
        form = self.get_filter_form()
        if form is not None and form.is_valid():
            queryset = form.filter(queryset)
        self._filter_form = form
        return queryset.order_by("-rating", "name")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["statistics"] = library_statistics()
        context["page_section_label"] = _("Развитие")
        return context


class ResourceDetailView(PageMetaMixin, BreadcrumbMixin, DetailView):
    """Карточка учебного ресурса."""

    template_name = "development/resource_detail.html"
    model = LearningResource
    context_object_name = "resource"
    page_section = "development"
    page_title = _("Учебный ресурс")

    def get_queryset(self):
        return LearningResource.objects.prefetch_related("competencies")

    def get_page_title(self) -> str:
        return self.object.name

    def get_breadcrumbs(self, trail: BreadcrumbTrail) -> BreadcrumbTrail:
        return trail.add(_("Библиотека развития"), "development:resources").add(self.object.name)

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_section_label"] = _("Развитие")
        # Перечень ограничен планами, доступными пользователю.
        context["activities"] = (
            DevelopmentActivity.objects.filter(
                resource=self.object,
                plan__in=plans_for(current_user(self.request)).values("pk"),
            )
            .select_related("plan__employee")
            .order_by("-due_date")[:10]
        )
        return context
