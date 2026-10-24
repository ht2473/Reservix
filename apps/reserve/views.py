"""Представления кадрового резерва."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.views.generic import DetailView, ListView, TemplateView
from django.views.generic.edit import UpdateView

from apps.org.models import Department, Position
from apps.people.models import Employee
from apps.reserve.forms import (
    CandidateSearchForm,
    HrDecisionForm,
    NominationDecisionForm,
    NominationFilterForm,
    NominationForm,
    ReserveEntryForm,
    ReserveFilterForm,
    SelfNominationForm,
    StatusChangeForm,
)
from apps.reserve.models import (
    NominationKind,
    NominationStatus,
    ReserveEntry,
    ReserveGroup,
    ReserveNomination,
    ReserveStatus,
)
from apps.reserve.services import (
    RESERVE_SECTION_ROLES,
    ReserveError,
    can_open_entry,
    change_status,
    check_eligibility,
    decide_hr,
    decide_manager,
    entries_due_for_review,
    pending_for_user,
    position_bench,
    rank_candidates,
    readiness_signal,
    refresh_readiness,
    reserve_summary,
    schedule_review,
    submit_nomination,
    target_profile,
    withdraw_nomination,
)
from core.constants import ReadinessLevel, Role
from core.mixins import (
    AuditedFormMixin,
    AuthenticatedRequest,
    BreadcrumbMixin,
    FilterMixin,
    PageMetaMixin,
    RoleRequiredMixin,
    SortableMixin,
    WindowedPaginationMixin,
)
from core.permissions import (
    apply_scope_by_path,
    can_approve_nomination,
    can_manage_reserve_entry,
    can_view_employee,
    current_user,
    is_privileged,
)
from core.settings_types import get_reservix_settings

if TYPE_CHECKING:
    from apps.accounts.models import User

#: Роли, ведущие кадровый резерв.
CURATOR_ROLES: tuple[str, ...] = (Role.ADMIN, Role.HR)

#: Роли, которым доступен раздел резерва в целом.
RESERVE_ROLES: tuple[str, ...] = tuple(RESERVE_SECTION_ROLES)

#: Роли, участвующие в выдвижении: сотрудник подаёт заявку на себя.
NOMINATION_ROLES: tuple[str, ...] = (Role.ADMIN, Role.HR, Role.MANAGER, Role.EMPLOYEE)


class ReserveBaseMixin(RoleRequiredMixin, PageMetaMixin, BreadcrumbMixin):
    """Общее поведение страниц раздела: доступ, заголовки, цепочка навигации."""

    allowed_roles = RESERVE_ROLES
    page_section = "reserve"
    permission_denied_message = _("Раздел кадрового резерва недоступен для вашей роли")

    def get_breadcrumbs(self, trail):
        # Корень цепочки — обзорная страница раздела, доступная этой роли.
        role = self.request.user.role
        if role in CURATOR_ROLES:
            root = "reserve:list"
        elif role == Role.MANAGER:
            root = "reserve:team"
        else:
            root = "reserve:apply"
        trail.add(_("Кадровый резерв"), root)
        title = getattr(self, "page_title", "")
        if title and self.request.resolver_match.url_name not in {"list", "team", "apply"}:
            trail.add(str(title))
        return trail

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_section_label"] = _("Кадровый резерв")
        context["is_curator"] = self.request.user.role in CURATOR_ROLES
        return context


# ---------------------------------------------------------------------------
# Список резерва
# ---------------------------------------------------------------------------
class ReserveListView(
    ReserveBaseMixin,
    FilterMixin,
    SortableMixin,
    WindowedPaginationMixin,
    ListView,
):
    """Кадровый резерв организации."""

    template_name = "reserve/index.html"
    model = ReserveEntry
    context_object_name = "entries"
    paginate_by = 25
    allowed_roles = CURATOR_ROLES
    page_title = _("Кадровый резерв")
    page_subtitle = _("Записи «резервист — целевая должность» с индексом готовности")
    filter_form_class = ReserveFilterForm
    sort_fields = {
        "employee": "employee__last_name",
        "position": "target_position__name",
        "readiness": "-readiness_index",
        "included": "-inclusion_date",
        "review": "review_date",
    }
    default_sort = ""

    def get_queryset(self):
        # Выборка строится целиком здесь: область видимости обязана быть применена до фильтра
        # формы, иначе отбор по подразделению вернул бы записи, которых пользователь не должен
        # видеть, — фильтр сужал бы уже неограниченный набор.
        queryset = apply_scope_by_path(
            ReserveEntry.objects.with_related(),
            current_user(self.request),
            prefix="employee",
        )
        form = self.get_filter_form()
        if form is not None and form.is_valid():
            queryset = form.filter(queryset)
        self._filter_form = form
        return self.apply_sort(queryset)

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["summary"] = reserve_summary(user=current_user(self.request))
        context["readiness_levels"] = ReadinessLevel.choices
        context["statuses"] = ReserveStatus.choices
        return context


class TeamReserveView(ReserveBaseMixin, WindowedPaginationMixin, ListView):
    """Резервисты подразделения — рабочее место руководителя."""

    template_name = "reserve/team.html"
    model = ReserveEntry
    context_object_name = "entries"
    paginate_by = 25
    allowed_roles = (Role.MANAGER, Role.ADMIN, Role.HR)
    page_title = _("Мои резервисты")
    page_subtitle = _("Сотрудники подразделения, состоящие в кадровом резерве")

    def get_queryset(self):
        queryset = ReserveEntry.objects.in_reserve().with_related()
        queryset = apply_scope_by_path(queryset, current_user(self.request), prefix="employee")
        return queryset.order_by("-readiness_index", "employee__last_name")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["summary"] = reserve_summary(user=current_user(self.request))
        context["pending_nominations"] = pending_for_user(current_user(self.request)).count()
        return context


class ReviewQueueView(ReserveBaseMixin, WindowedPaginationMixin, ListView):
    """Очередь пересмотра резерва."""

    template_name = "reserve/reviews.html"
    model = ReserveEntry
    context_object_name = "entries"
    paginate_by = 25
    allowed_roles = CURATOR_ROLES
    page_title = _("Пересмотр состава резерва")
    page_subtitle = _("Записи, по которым наступил срок очередного пересмотра")

    def get_queryset(self):
        return entries_due_for_review(current_user(self.request))


class ReserveEntryDetailView(ReserveBaseMixin, DetailView):
    """Карточка записи резерва."""

    template_name = "reserve/entry_detail.html"
    model = ReserveEntry
    context_object_name = "entry"
    slug_field = "public_id"
    slug_url_kwarg = "public_id"
    page_title = _("Запись кадрового резерва")

    def get_queryset(self):
        return ReserveEntry.objects.with_related()

    def get_object(self, queryset=None) -> ReserveEntry:
        entry = super().get_object(queryset)
        if not can_view_employee(current_user(self.request), entry.employee):
            raise PermissionDenied(_("Запись относится к сотруднику вне вашей области видимости"))
        return entry

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        entry: ReserveEntry = self.object

        profile = target_profile(entry)
        context["profile"] = profile
        context["priorities"] = profile.development_priorities(5) if profile else ()
        context["blocking_gaps"] = profile.blocking_gaps if profile else ()
        context["history"] = entry.status_history.select_related("changed_by").all()[:20]
        context["memberships"] = entry.memberships.select_related("group").all()
        context["nomination"] = getattr(entry, "nomination", None)
        context["bench"] = position_bench(entry.target_position_id, current_user(self.request))
        context["can_manage"] = can_manage_reserve_entry(current_user(self.request), entry)
        context["status_form"] = StatusChangeForm(entry=entry)

        # Пороги передаются в шкалу готовности значениями настроек, а не константами разметки:
        # кадровая служба пересматривает их вслед за кадровой политикой, и шкала обязана следовать
        # за настройкой.
        options = get_reservix_settings()
        context["readiness_signal"] = readiness_signal(entry.readiness_level)
        context["threshold_high"] = options["READINESS_THRESHOLD_HIGH"]
        context["threshold_medium"] = options["READINESS_THRESHOLD_MEDIUM"]
        return context


class ReserveEntryUpdateView(ReserveBaseMixin, AuditedFormMixin, UpdateView):
    """Изменение записи резерва кадровой службой."""

    template_name = "reserve/entry_form.html"
    model = ReserveEntry
    form_class = ReserveEntryForm
    slug_field = "public_id"
    slug_url_kwarg = "public_id"
    allowed_roles = CURATOR_ROLES
    page_title = _("Изменение записи резерва")

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, _("Изменения сохранены"))
        return response


@login_required
def change_status_view(request: AuthenticatedRequest, public_id) -> HttpResponse:
    """Переводит запись резерва в другое состояние."""
    entry = get_object_or_404(ReserveEntry.objects.with_related(), public_id=public_id)
    if not can_manage_reserve_entry(request.user, entry):
        raise PermissionDenied(_("Изменение записи доступно кадровой службе"))

    form = StatusChangeForm(request.POST or None, entry=entry)

    if request.method == "POST" and form.is_valid():
        try:
            change_status(
                entry,
                form.cleaned_data["status"],
                actor=request.user,
                reason=form.cleaned_data["reason"],
            )
        except ReserveError as error:
            messages.error(request, str(error))
        else:
            messages.success(
                request,
                _("Состояние изменено: %(status)s") % {"status": entry.get_status_display()},
            )
            return redirect(entry.get_absolute_url())

    return render(
        request,
        "reserve/entry_status.html",
        {
            "entry": entry,
            "form": form,
            "page_title": _("Изменение состояния записи"),
            "page_section_label": _("Кадровый резерв"),
        },
    )


@login_required
def refresh_readiness_view(request: AuthenticatedRequest, public_id) -> HttpResponse:
    """Пересчитывает индекс готовности одной записи по требованию куратора."""
    entry = get_object_or_404(ReserveEntry.objects.with_related(), public_id=public_id)
    if not can_manage_reserve_entry(request.user, entry):
        raise PermissionDenied(_("Действие доступно кадровой службе"))

    if request.method != "POST":
        return redirect(entry.get_absolute_url())

    refresh_readiness(entry, actor=request.user)
    messages.success(
        request,
        _("Индекс пересчитан: %(index)s (%(level)s)")
        % {
            "index": entry.readiness_index or "—",
            "level": entry.get_readiness_level_display(),
        },
    )
    return redirect(entry.get_absolute_url())


@login_required
def schedule_review_view(request: AuthenticatedRequest, public_id) -> HttpResponse:
    """Переносит срок очередного пересмотра записи."""
    entry = get_object_or_404(ReserveEntry.objects.with_related(), public_id=public_id)
    if not can_manage_reserve_entry(request.user, entry):
        raise PermissionDenied(_("Действие доступно кадровой службе"))

    if request.method != "POST":
        return redirect(entry.get_absolute_url())

    schedule_review(entry, actor=request.user)
    messages.success(
        request,
        _("Пересмотр назначен на %(date)s") % {"date": entry.review_date},
    )
    return redirect(request.POST.get("next") or entry.get_absolute_url())


# ---------------------------------------------------------------------------
# Заявки на выдвижение
# ---------------------------------------------------------------------------
class NominationListView(ReserveBaseMixin, FilterMixin, WindowedPaginationMixin, ListView):
    """Заявки на включение в резерв в границах видимости пользователя."""

    template_name = "reserve/nominations.html"
    model = ReserveNomination
    context_object_name = "nominations"
    paginate_by = 25
    allowed_roles = RESERVE_ROLES
    page_title = _("Заявки на выдвижение")
    page_subtitle = _("Обоснование, маршрут согласования и решения по каждой заявке")
    filter_form_class = NominationFilterForm

    def get_queryset(self):
        # Подразделение кандидата выводится в каждой строке, а `with_related` модели его не
        # забирает: без этой связи список из двадцати пяти заявок обходился бы тридцатью четырьмя
        # запросами вместо девяти.
        queryset = apply_scope_by_path(
            ReserveNomination.objects.with_related().select_related("employee__department"),
            current_user(self.request),
            prefix="employee",
        )
        form = self.get_filter_form()
        if form is not None and form.is_valid():
            queryset = form.filter(queryset)
        self._filter_form = form
        return queryset

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["pending_count"] = pending_for_user(current_user(self.request)).count()
        return context


class ApprovalsView(ReserveBaseMixin, WindowedPaginationMixin, ListView):
    """Заявки, ожидающие решения текущего пользователя."""

    template_name = "reserve/approvals.html"
    model = ReserveNomination
    context_object_name = "nominations"
    paginate_by = 25
    allowed_roles = RESERVE_ROLES
    page_title = _("Согласование заявок")
    page_subtitle = _("Заявки, ожидающие вашего решения")

    def get_queryset(self):
        return pending_for_user(current_user(self.request))


class NominationDetailView(ReserveBaseMixin, DetailView):
    """Карточка заявки: обоснование, соответствие кандидата, ход согласования."""

    template_name = "reserve/nomination_detail.html"
    model = ReserveNomination
    context_object_name = "nomination"
    slug_field = "public_id"
    slug_url_kwarg = "public_id"
    allowed_roles = NOMINATION_ROLES
    page_title = _("Заявка на выдвижение")

    def get_queryset(self):
        return ReserveNomination.objects.with_related().select_related(
            "employee__department", "target_position__grade", "reserve_group", "entry"
        )

    def get_object(self, queryset=None) -> ReserveNomination:
        nomination = super().get_object(queryset)
        user = current_user(self.request)
        is_own = nomination.employee.user_id == user.pk or nomination.nominated_by_id == user.pk
        if not is_own and not can_view_employee(user, nomination.employee):
            raise PermissionDenied(_("Заявка относится к сотруднику вне вашей области видимости"))
        return nomination

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        nomination: ReserveNomination = self.object
        user = current_user(self.request)

        eligibility = check_eligibility(
            nomination.employee,
            nomination.target_position,
            group=nomination.reserve_group,
        )
        context["eligibility"] = eligibility
        context["profile"] = eligibility.profile
        context["priorities"] = (
            eligibility.profile.development_priorities(5) if eligibility.profile else ()
        )
        context["bench"] = position_bench(nomination.target_position_id, current_user(self.request))

        stage = _decision_stage(user, nomination)
        context["decision_stage"] = stage
        if stage == "manager":
            context["decision_form"] = NominationDecisionForm()
        elif stage == "hr":
            context["decision_form"] = HrDecisionForm(
                initial={"reserve_group": nomination.reserve_group_id}
            )
        context["can_withdraw"] = nomination.is_pending and (
            nomination.nominated_by_id == user.pk or is_privileged(user)
        )
        # Заявку видит и сотрудник, подавший её на себя, а раздел резерва
        # для его роли закрыт: ссылка на созданную запись вела на отказ.
        context["can_open_entry"] = can_open_entry(user, nomination.entry)
        return context


def _decision_stage(user: User, nomination: ReserveNomination) -> str:
    """Определяет, чьё решение по заявке ожидается от текущего пользователя."""
    if nomination.status == NominationStatus.MANAGER_REVIEW and can_approve_nomination(
        user, nomination
    ):
        return "manager"
    if nomination.status == NominationStatus.HR_REVIEW and user.role in CURATOR_ROLES:
        return "hr"
    return ""


class NominationCreateView(ReserveBaseMixin, TemplateView):
    """Выдвижение сотрудника руководителем или кадровой службой."""

    template_name = "reserve/nomination_form.html"
    allowed_roles = RESERVE_ROLES
    page_title = _("Выдвижение в кадровый резерв")
    page_subtitle = _("Заявка проходит согласование руководителем и кадровой службой")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        # Начальные значения берутся из адреса: переход из подбора кандидатов приносит с собой
        # сотрудника и должность, и переспрашивать их у пользователя, только что их выбравшего,
        # незачем.
        initial = {
            key: value
            for key, value in self.request.GET.items()
            if key in {"employee", "target_position", "reserve_group"}
        }
        context.setdefault("form", NominationForm(initial=initial, user=self.request.user))
        return context

    def post(self, request: AuthenticatedRequest, *args, **kwargs) -> HttpResponse:
        form = NominationForm(request.POST, user=request.user)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))

        nomination = form.save(commit=False)
        nomination.nominated_by = request.user
        nomination.kind = (
            NominationKind.HR if request.user.role in CURATOR_ROLES else NominationKind.MANAGER
        )
        nomination.save()

        try:
            submit_nomination(nomination, actor=request.user)
        except ReserveError as error:
            nomination.delete()
            form.add_error(None, str(error))
            return self.render_to_response(self.get_context_data(form=form))

        messages.success(request, _("Заявка подана и направлена на согласование"))
        return redirect(nomination.get_absolute_url())


class ApplyView(ReserveBaseMixin, TemplateView):
    """Самовыдвижение сотрудника."""

    template_name = "reserve/apply.html"
    allowed_roles = (Role.EMPLOYEE, Role.MANAGER, Role.MENTOR, Role.ADMIN, Role.HR)
    page_title = _("Заявка в кадровый резерв")
    page_subtitle = _("Самовыдвижение с обоснованием готовности к целевой должности")

    def get_employee(self) -> Employee:
        employee = Employee.objects.with_org().filter(user_id=self.request.user.pk).first()
        if employee is None:
            raise PermissionDenied(
                _("Учётная запись не связана с карточкой сотрудника: заявка невозможна")
            )
        return employee

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        employee = self.get_employee()
        context["employee"] = employee
        context.setdefault("form", SelfNominationForm(employee=employee))
        context["my_nominations"] = (
            ReserveNomination.objects.for_employee(employee.pk)
            .with_related()
            .order_by("-created_at")[:10]
        )
        context["my_entries"] = (
            ReserveEntry.objects.for_employee(employee.pk)
            .with_related()
            .order_by("-inclusion_date")
        )
        return context

    def post(self, request: AuthenticatedRequest, *args, **kwargs) -> HttpResponse:
        employee = self.get_employee()
        form = SelfNominationForm(request.POST, employee=employee)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))

        nomination = form.save(commit=False)
        nomination.nominated_by = request.user
        nomination.save()

        try:
            submit_nomination(nomination, actor=request.user)
        except ReserveError as error:
            nomination.delete()
            form.add_error(None, str(error))
            return self.render_to_response(self.get_context_data(form=form))

        messages.success(request, _("Заявка подана. Её рассмотрят руководитель и кадровая служба"))
        return redirect("reserve:apply")


@login_required
def decide_nomination_view(request: AuthenticatedRequest, public_id) -> HttpResponse:
    """Принимает решение согласующего по заявке."""
    nomination = get_object_or_404(ReserveNomination.objects.with_related(), public_id=public_id)

    if request.method != "POST":
        return redirect(nomination.get_absolute_url())

    stage = _decision_stage(request.user, nomination)
    if not stage:
        raise PermissionDenied(_("Решение по этой заявке принимаете не вы"))

    form_class = HrDecisionForm if stage == "hr" else NominationDecisionForm
    form = form_class(request.POST)
    if not form.is_valid():
        for error in form.errors.values():
            messages.error(request, "; ".join(str(item) for item in error))
        return redirect(nomination.get_absolute_url())

    comment = form.cleaned_data["comment"]
    try:
        if stage == "manager":
            decide_manager(nomination, actor=request.user, approve=form.approved, comment=comment)
        else:
            decide_hr(
                nomination,
                actor=request.user,
                approve=form.approved,
                comment=comment,
                group=form.cleaned_data.get("reserve_group"),
                review_months=form.cleaned_data.get("review_months") or 24,
            )
    except ReserveError as error:
        messages.error(request, str(error))
        return redirect(nomination.get_absolute_url())

    if form.approved and nomination.entry_id:
        messages.success(request, _("Заявка одобрена, кандидат включён в резерв"))
        return redirect(nomination.entry.get_absolute_url())

    messages.success(
        request,
        _("Решение сохранено: %(status)s") % {"status": nomination.get_status_display()},
    )
    return redirect(nomination.get_absolute_url())


@login_required
def withdraw_nomination_view(request: AuthenticatedRequest, public_id) -> HttpResponse:
    """Отзывает поданную заявку."""
    nomination = get_object_or_404(ReserveNomination.objects.with_related(), public_id=public_id)

    if request.method != "POST":
        return redirect(nomination.get_absolute_url())

    if nomination.nominated_by_id != request.user.pk and not is_privileged(request.user):
        raise PermissionDenied(_("Отозвать заявку может её инициатор либо кадровая служба"))

    try:
        withdraw_nomination(nomination, actor=request.user, reason=request.POST.get("reason", ""))
    except ReserveError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, _("Заявка отозвана"))
    return redirect(nomination.get_absolute_url())


# ---------------------------------------------------------------------------
# Программы подготовки
# ---------------------------------------------------------------------------
class ReserveGroupListView(ReserveBaseMixin, ListView):
    """Программы подготовки резерва."""

    template_name = "reserve/groups.html"
    model = ReserveGroup
    context_object_name = "groups"
    allowed_roles = RESERVE_ROLES
    page_title = _("Программы подготовки")
    page_subtitle = _("Виды резерва с правилами зачисления и горизонтом подготовки")

    def get_queryset(self):
        return (
            ReserveGroup.objects.filter(is_active=True)
            .select_related("curator")
            .annotate(
                members=Count("memberships", filter=Q(memberships__left_at__isnull=True)),
                entries_total=Count("entries", distinct=True),
            )
            .order_by("sort_order", "name")
        )


class ReserveGroupDetailView(ReserveBaseMixin, DetailView):
    """Состав и правила одной программы подготовки."""

    template_name = "reserve/group_detail.html"
    model = ReserveGroup
    context_object_name = "group"
    allowed_roles = RESERVE_ROLES
    page_title = _("Программа подготовки")

    def get_queryset(self):
        return ReserveGroup.objects.select_related("curator")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        group: ReserveGroup = self.object
        # Состав программы показывается целиком: программа подготовки объединяет резервистов разных
        # подразделений, и урезанный список исказил бы её наполнение.
        user = current_user(self.request)
        memberships = list(
            group.memberships.filter(left_at__isnull=True)
            .select_related("employee", "employee__department", "entry", "entry__target_position")
            .order_by("employee__last_name")
        )
        for membership in memberships:
            membership.can_open_entry = can_open_entry(user, membership.entry)
        context["memberships"] = memberships
        context["entries"] = (
            ReserveEntry.objects.filter(reserve_group=group).in_reserve().with_related()
        )
        context["page_title"] = group.name
        return context


# ---------------------------------------------------------------------------
# Подбор кандидатов
# ---------------------------------------------------------------------------
class CandidateSearchView(ReserveBaseMixin, TemplateView):
    """Подбор кандидатов на должность по соответствию требованиям."""

    template_name = "reserve/candidates.html"
    allowed_roles = CURATOR_ROLES
    page_title = _("Подбор кандидатов")
    page_subtitle = _("Сотрудники, наиболее соответствующие требованиям должности")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        form = CandidateSearchForm(self.request.GET or None, user=self.request.user)
        context["form"] = form

        if not self.request.GET or not form.is_valid():
            return context

        position: Position = form.cleaned_data["position"]
        department: Department | None = form.cleaned_data.get("department")
        min_match = form.cleaned_data.get("min_match") or 0

        pool = Employee.objects.reserve_eligible()
        pool = apply_scope_by_path(pool, current_user(self.request), prefix="")
        if department is not None:
            pool = pool.filter(department_id__in=Department.objects.descendant_ids(department.pk))

        context["position"] = position
        context["candidates"] = rank_candidates(position, pool=pool, min_match=float(min_match))
        context["bench"] = position_bench(position.pk, current_user(self.request))
        return context
