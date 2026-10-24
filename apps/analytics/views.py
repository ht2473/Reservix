"""Представления аналитического ядра."""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q, QuerySet
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.views.generic import DetailView, ListView, TemplateView
from django.views.generic.edit import CreateView, UpdateView

from apps.analytics.forms import (
    AHPJudgementForm,
    CalibrationCloseForm,
    CalibrationFilterForm,
    CalibrationForm,
    CalibrationSessionForm,
    EntropyForm,
    ExpertWeightForm,
    NineBoxFilterForm,
    PlacementForm,
    RankingFilterForm,
    RecalculationForm,
    SchemeComparisonForm,
    SchemeFilterForm,
    SchemeForm,
    StabilityForm,
)
from apps.analytics.models import (
    CalibrationSession,
    NineBoxPlacement,
    ReadinessSnapshot,
    ScoringCriterion,
    ScoringScheme,
)
from apps.analytics.services import (
    AnalyticsError,
    auto_place,
    build_ahp_weights,
    build_entropy_weights,
    calibrate,
    calibration_effect,
    compare_schemes,
    complete_calibration,
    criterion_averages,
    ensure_criteria,
    explain,
    index_distribution,
    nine_box_grid,
    place_employee,
    readiness_forecast,
    recalculate,
    reserve_health,
    stability,
)
from apps.people.models import Employee
from apps.reserve.models import ReserveEntry
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
from core.permissions import apply_scope_by_path, current_user

#: Роли, ведущие методику: правка весов, построение схем, запуск пересчёта.
METHODOLOGIST_ROLES: tuple[str, ...] = (Role.ADMIN, Role.HR)

#: Роли, которым доступен просмотр раздела.
VIEWER_ROLES: tuple[str, ...] = (Role.ADMIN, Role.HR, Role.MANAGER)

#: Предел выборки для страниц, считающих на лету.
ANALYSIS_LIMIT = 400


class AnalyticsBaseMixin(RoleRequiredMixin, PageMetaMixin, BreadcrumbMixin):
    """Общее поведение страниц аналитического ядра."""

    allowed_roles = VIEWER_ROLES
    page_section = "analytics"
    permission_denied_message = _("Раздел аналитики доступен кадровой службе и руководителям")

    def get_breadcrumbs(self, trail):
        trail.add(_("Аналитика"), "analytics:index")
        title = getattr(self, "page_title", "")
        if title and self.request.resolver_match.url_name != "index":
            trail.add(str(title))
        return trail

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_section_label"] = _("Аналитика")
        context["can_edit_methodology"] = self.request.user.role in METHODOLOGIST_ROLES
        context["default_scheme"] = ScoringScheme.objects.with_weights().default()
        return context


# ---------------------------------------------------------------------------
# Обзор
# ---------------------------------------------------------------------------
class AnalyticsOverviewView(AnalyticsBaseMixin, TemplateView):
    """Обзор аналитического ядра."""

    template_name = "analytics/index.html"
    page_title = _("Аналитика резерва")
    page_subtitle = _("Индекс готовности, распределение и результативность подготовки")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        user = current_user(self.request)

        context["health"] = reserve_health(user=user)
        context["distribution"] = index_distribution(
            apply_scope_by_path(ReserveEntry.objects.in_reserve(), user, prefix="employee")
        )
        context["criteria"] = criterion_averages()
        context["grid"] = nine_box_grid(with_people=False)
        context["last_calculation"] = (
            ReadinessSnapshot.objects.filter(is_current=True).order_by("-calculated_at").first()
        )
        context["schemes"] = ScoringScheme.objects.with_weights().filter(is_active=True)
        context["top"] = (
            apply_scope_by_path(ReserveEntry.objects.in_reserve(), user, prefix="employee")
            .with_related()
            .exclude(readiness_index__isnull=True)
            .order_by("-readiness_index")[:10]
        )
        return context


class RankingView(AnalyticsBaseMixin, FilterMixin, WindowedPaginationMixin, ListView):
    """Рейтинг резервистов по индексу готовности."""

    template_name = "analytics/ranking.html"
    model = ReadinessSnapshot
    context_object_name = "snapshots"
    paginate_by = 25
    page_title = _("Рейтинг готовности")
    page_subtitle = _("Резервисты в порядке композитного индекса с разбором по критериям")
    filter_form_class = RankingFilterForm

    def get_queryset(self):
        scheme = self._scheme()
        queryset = apply_scope_by_path(
            ReadinessSnapshot.objects.filter(is_current=True).with_related(),
            current_user(self.request),
            prefix="entry__employee",
        ).select_related("entry__target_position", "entry__employee__department")
        if scheme is not None:
            queryset = queryset.filter(scheme=scheme)

        form = self.get_filter_form()
        if form is not None and form.is_valid():
            queryset = form.filter(queryset)
        self._filter_form = form
        return queryset.order_by("-index_value")

    def _scheme(self) -> ScoringScheme | None:
        """Схема, по которой показывается рейтинг: указанная либо по умолчанию."""
        code = self.request.GET.get("scheme")
        if code:
            return ScoringScheme.objects.filter(code=code).first()
        return ScoringScheme.objects.default()

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["scheme"] = self._scheme()
        context["schemes"] = ScoringScheme.objects.filter(is_active=True)

        # Отбор по достоверности выполняется здесь, а не выборкой: доля обеспеченного веса лежит
        # внутри поля JSON, и условие по ней в выборке пришлось бы выражать поиском по тексту.
        reliability = self.request.GET.get("reliability")
        if reliability in {"reliable", "partial"}:
            wanted = reliability == "reliable"
            context["snapshots"] = [
                snapshot for snapshot in context["snapshots"] if _is_reliable(snapshot) is wanted
            ]
        return context


def _is_reliable(snapshot: ReadinessSnapshot) -> bool:
    """Признак достаточной обеспеченности расчёта данными."""
    meta = snapshot.criterion_values.get("_meta") if snapshot.criterion_values else None
    return bool(meta and meta.get("reliable"))


class EntryBreakdownView(AnalyticsBaseMixin, DetailView):
    """Разбор индекса готовности одной записи резерва."""

    template_name = "analytics/breakdown.html"
    model = ReserveEntry
    context_object_name = "entry"
    slug_field = "public_id"
    slug_url_kwarg = "public_id"
    page_title = _("Разбор индекса готовности")

    def get_queryset(self):
        return apply_scope_by_path(
            ReserveEntry.objects.with_related(), current_user(self.request), prefix="employee"
        )

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        entry: ReserveEntry = self.object
        scheme = (
            ScoringScheme.objects.filter(code=self.request.GET.get("scheme", "")).first()
            or ScoringScheme.objects.with_weights().default()
        )

        try:
            context["breakdown"] = explain(entry, scheme)
        except AnalyticsError as error:
            context["breakdown"] = None
            context["error"] = str(error)

        context["scheme"] = scheme
        context["schemes"] = ScoringScheme.objects.filter(is_active=True)
        context["forecast"] = readiness_forecast(entry)
        context["history"] = (
            ReadinessSnapshot.objects.filter(entry=entry)
            .select_related("scheme")
            .order_by("-calculated_at")[:12]
        )
        context["placement"] = NineBoxPlacement.objects.filter(
            employee=entry.employee, is_current=True
        ).first()
        context["page_title"] = str(entry.employee.short_name)
        return context


# ---------------------------------------------------------------------------
# Критерии и схемы оценивания
# ---------------------------------------------------------------------------
class CriterionListView(AnalyticsBaseMixin, ListView):
    """Справочник частных критериев индекса."""

    template_name = "analytics/criteria.html"
    model = ScoringCriterion
    context_object_name = "criteria"
    page_title = _("Критерии готовности")
    page_subtitle = _("Шесть частных показателей композитного индекса и способ их расчёта")

    def get_queryset(self):
        return ScoringCriterion.objects.all().order_by("sort_order")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        from apps.analytics.criteria import EVALUATORS

        context = super().get_context_data(**kwargs)
        context["averages"] = {row["code"]: row for row in criterion_averages()}
        context["implemented"] = set(EVALUATORS)
        return context


class CriterionDetailView(AnalyticsBaseMixin, DetailView):
    """Карточка критерия: методика, единицы, участие в схемах."""

    template_name = "analytics/criterion_detail.html"
    model = ScoringCriterion
    context_object_name = "criterion"
    page_title = _("Критерий готовности")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        from apps.analytics.criteria import EVALUATORS

        context = super().get_context_data(**kwargs)
        criterion: ScoringCriterion = self.object
        context["weights"] = criterion.weights.select_related("scheme").order_by("scheme__name")
        context["average"] = next(
            (row for row in criterion_averages() if row["code"] == criterion.code), None
        )
        context["is_implemented"] = criterion.source in EVALUATORS
        context["page_title"] = criterion.name
        return context


class SchemeListView(AnalyticsBaseMixin, FilterMixin, ListView):
    """Схемы оценивания: три способа получения весов рядом."""

    template_name = "analytics/schemes.html"
    model = ScoringScheme
    context_object_name = "schemes"
    page_title = _("Схемы оценивания")
    page_subtitle = _("Экспертные веса, метод анализа иерархий и энтропийный метод")
    filter_form_class = SchemeFilterForm

    def get_queryset(self):
        queryset = ScoringScheme.objects.with_weights()
        form = self.get_filter_form()
        if form is not None and form.is_valid():
            queryset = form.filter(queryset)
        self._filter_form = form
        return queryset

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["criteria"] = ScoringCriterion.objects.filter(is_active=True).order_by("sort_order")
        context["recalculation_form"] = RecalculationForm()
        return context


class SchemeDetailView(AnalyticsBaseMixin, DetailView):
    """Карточка схемы: веса, происхождение, согласованность суждений."""

    template_name = "analytics/scheme_detail.html"
    model = ScoringScheme
    context_object_name = "scheme"
    page_title = _("Схема оценивания")

    def get_queryset(self):
        return ScoringScheme.objects.with_weights()

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        scheme: ScoringScheme = self.object
        context["weights"] = scheme.weights.select_related("criterion").order_by(
            "criterion__sort_order"
        )
        context["snapshots"] = ReadinessSnapshot.objects.filter(
            scheme=scheme, is_current=True
        ).count()
        context["matrix"] = _pairwise_rows(scheme)
        context["page_title"] = scheme.name
        return context


def _pairwise_rows(scheme: ScoringScheme) -> list[dict[str, Any]] | None:
    """Готовит матрицу парных сравнений к выводу таблицей."""
    payload = scheme.pairwise_matrix
    if not isinstance(payload, dict) or "matrix" not in payload:
        return None

    codes = payload.get("criteria", [])
    names = dict(ScoringCriterion.objects.filter(code__in=codes).values_list("code", "name"))
    return [
        {"label": names.get(code, code), "values": row}
        for code, row in zip(codes, payload["matrix"], strict=False)
    ]


class SchemeCreateView(AnalyticsBaseMixin, AuditedFormMixin, CreateView):
    """Создание схемы оценивания."""

    template_name = "analytics/scheme_form.html"
    model = ScoringScheme
    form_class = SchemeForm
    allowed_roles = METHODOLOGIST_ROLES
    page_title = _("Новая схема оценивания")

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        response = super().form_valid(form)
        messages.success(self.request, _("Схема создана. Задайте веса критериев"))
        return response


class SchemeUpdateView(AnalyticsBaseMixin, AuditedFormMixin, UpdateView):
    """Правка описания схемы. Веса задаются отдельными страницами."""

    template_name = "analytics/scheme_form.html"
    model = ScoringScheme
    form_class = SchemeForm
    allowed_roles = METHODOLOGIST_ROLES
    page_title = _("Правка схемы оценивания")

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs


@login_required
def expert_weights_view(request: AuthenticatedRequest, pk: int) -> HttpResponse:
    """Экспертное назначение весов критериев схемы."""
    _require_methodologist(request)
    scheme = get_object_or_404(ScoringScheme.objects.with_weights(), pk=pk)
    criteria = list(ScoringCriterion.objects.filter(is_active=True).order_by("sort_order"))

    if request.method == "POST":
        form = ExpertWeightForm(request.POST, criteria=criteria)
        if form.is_valid():
            from apps.analytics.services import apply_expert_weights

            try:
                apply_expert_weights(scheme, form.weights())
            except AnalyticsError as error:
                messages.error(request, str(error))
            else:
                messages.success(request, _("Веса сохранены и приведены к сумме единицы"))
                return redirect(scheme.get_absolute_url())
    else:
        current = {
            weight.criterion_id: (weight.raw_value or weight.weight)
            for weight in scheme.weights.all()
        }
        form = ExpertWeightForm(
            initial={
                f"weight_{criterion.pk}": current.get(criterion.pk, 1) for criterion in criteria
            },
            criteria=criteria,
        )

    return render(
        request,
        "analytics/scheme_weights.html",
        {
            "scheme": scheme,
            "form": form,
            "criteria": criteria,
            "page_title": _("Экспертные веса"),
            "page_section": "analytics",
        },
    )


@login_required
def ahp_view(request: AuthenticatedRequest, pk: int) -> HttpResponse:
    """Построение весов методом анализа иерархий."""
    _require_methodologist(request)
    scheme = get_object_or_404(ScoringScheme.objects.with_weights(), pk=pk)
    criteria = list(ScoringCriterion.objects.filter(is_active=True).order_by("sort_order"))
    outcome = None

    if request.method == "POST":
        form = AHPJudgementForm(request.POST, criteria=criteria)
        if form.is_valid():
            try:
                outcome = build_ahp_weights(
                    scheme,
                    form.judgements(),
                    criteria,
                    note=form.cleaned_data.get("note", ""),
                )
            except AnalyticsError as error:
                messages.error(request, str(error))
            else:
                scheme.refresh_from_db()
                if scheme.is_consistent:
                    messages.success(
                        request,
                        _("Веса рассчитаны. Отношение согласованности — %(value)s")
                        % {"value": scheme.consistency_ratio},
                    )
                    return redirect(scheme.get_absolute_url())
                messages.warning(
                    request,
                    _(
                        "Суждения противоречивы: отношение согласованности %(value)s "
                        "выше порога %(limit)s. Веса сохранены, но схема к расчёту "
                        "не допускается."
                    )
                    % {
                        "value": scheme.consistency_ratio,
                        "limit": scheme.consistency_threshold,
                    },
                )
    else:
        form = AHPJudgementForm(criteria=criteria, initial=_ahp_initial(scheme, criteria))

    return render(
        request,
        "analytics/ahp.html",
        {
            "scheme": scheme,
            "form": form,
            "criteria": criteria,
            "outcome": outcome,
            "threshold": scheme.consistency_threshold,
            "page_title": _("Парные сравнения критериев"),
            "page_section": "analytics",
        },
    )


def _ahp_initial(scheme: ScoringScheme, criteria: list[ScoringCriterion]) -> dict[str, str]:
    """Восстанавливает суждения из сохранённой матрицы для повторного ввода."""
    from apps.analytics.forms import SAATY_CHOICES

    payload = scheme.pairwise_matrix
    if not isinstance(payload, dict) or "matrix" not in payload:
        return {}

    codes = payload.get("criteria", [])
    order = {code: index for index, code in enumerate(codes)}
    scale = [float(value) for value, _label in SAATY_CHOICES]
    initial: dict[str, str] = {}

    for first, first_criterion in enumerate(criteria):
        for second in range(first + 1, len(criteria)):
            row = order.get(first_criterion.code)
            column = order.get(criteria[second].code)
            if row is None or column is None:
                continue
            value = float(payload["matrix"][row][column])
            nearest = min(scale, key=lambda option: abs(option - value))
            initial[f"pair_{first}_{second}"] = next(
                option for option, _label in SAATY_CHOICES if float(option) == nearest
            )
    return initial


@login_required
def entropy_view(request: AuthenticatedRequest, pk: int) -> HttpResponse:
    """Построение весов энтропийным методом по данным резерва."""
    _require_methodologist(request)
    scheme = get_object_or_404(ScoringScheme.objects.with_weights(), pk=pk)
    criteria = list(ScoringCriterion.objects.filter(is_active=True).order_by("sort_order"))
    outcome = None

    if request.method == "POST":
        form = EntropyForm(request.POST)
        if form.is_valid():
            entries = ReserveEntry.objects.in_reserve()
            if form.cleaned_data.get("scope") == "ready":
                entries = entries.ready_now()
            if branch := form.cleaned_data.get("branch"):
                entries = entries.filter(employee__branch=branch)

            try:
                outcome = build_entropy_weights(
                    scheme,
                    criteria,
                    entries=entries,
                    note=form.cleaned_data.get("note", ""),
                )
            except AnalyticsError as error:
                messages.error(request, str(error))
            else:
                # Перенаправления нет намеренно: разбор по энтропиям — главный результат страницы,
                # и уводить с неё сразу после расчёта значит показать веса, скрыв основание, по
                # которому они получились именно такими.
                messages.success(
                    request,
                    _("Веса выведены из выборки в %(count)s записей") % {"count": outcome.sample},
                )
    else:
        form = EntropyForm()

    return render(
        request,
        "analytics/entropy.html",
        {
            "scheme": scheme,
            "form": form,
            "criteria": criteria,
            "outcome": outcome,
            "page_title": _("Энтропийные веса"),
            "page_section": "analytics",
        },
    )


@login_required
def recalculate_view(request: AuthenticatedRequest) -> HttpResponse:
    """Запуск пересчёта индексов готовности."""
    _require_methodologist(request)
    if request.method != "POST":
        return redirect("analytics:schemes")

    form = RecalculationForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Не удалось разобрать параметры пересчёта"))
        return redirect("analytics:schemes")

    try:
        result = recalculate(
            scheme=form.cleaned_data.get("scheme"),
            actor=request.user,
            notify_changes=form.cleaned_data.get("notify", True),
        )
    except AnalyticsError as error:
        messages.error(request, str(error))
        return redirect("analytics:schemes")

    if result.is_operational:
        messages.success(
            request,
            _(
                "Пересчитано записей: %(count)s. Средний индекс — %(index)s, "
                "уровень готовности изменился у %(levels)s."
            )
            % {
                "count": result.entries_updated,
                "index": result.average_index,
                "levels": result.level_changes,
            },
        )
    else:
        messages.success(
            request,
            _(
                "Снимков сохранено: %(count)s. Схема не является схемой по умолчанию, "
                "поэтому рабочие уровни готовности не изменены."
            )
            % {"count": result.snapshots_created},
        )
    if result.unreliable:
        messages.warning(
            request,
            _(
                "Записей с обеспеченностью данными ниже двух третей: %(count)s. "
                "Их индекс получен по неполному набору критериев."
            )
            % {"count": result.unreliable},
        )
    return redirect(request.POST.get("next") or "analytics:schemes")


# ---------------------------------------------------------------------------
# Сравнение схем и устойчивость
# ---------------------------------------------------------------------------
class ComparisonView(AnalyticsBaseMixin, TemplateView):
    """Сравнение ранжирований, полученных разными схемами весов."""

    template_name = "analytics/comparison.html"
    page_title = _("Сравнение схем оценивания")
    page_subtitle = _("Расходятся ли выводы, полученные разными способами назначения весов")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        form = SchemeComparisonForm(self.request.GET or None)
        context["form"] = form

        schemes = list(ScoringScheme.objects.with_weights().usable())
        selected = schemes
        top = 10
        if form.is_bound and form.is_valid():
            selected = list(form.cleaned_data["schemes"])
            top = form.cleaned_data["top"]

        if len(selected) < 2:
            context["comparison"] = None
            return context

        # Выборка ограничена областью видимости пользователя.
        entries = apply_scope_by_path(
            ReserveEntry.objects.in_reserve().with_related(),
            current_user(self.request),
            prefix="employee",
        )[:ANALYSIS_LIMIT]
        try:
            comparison = compare_schemes(selected, entries, top=top)
        except AnalyticsError as error:
            context["comparison"] = None
            context["error"] = str(error)
            return context

        context["comparison"] = comparison
        context["schemes"] = selected
        context["pairs"] = [
            {
                "left": key[0],
                "right": key[1],
                "correlation": value,
                "top_agreement": comparison.top_agreement.get(key, 0.0),
            }
            for key, value in comparison.correlations.items()
        ]
        context["volatile"] = _volatile_rows(comparison)
        context["sample"] = len(comparison.rankings[0].entry_ids) if comparison.rankings else 0
        return context


def _volatile_rows(comparison) -> list[dict[str, Any]]:
    """Резервисты с наибольшим расхождением рангов между схемами."""
    if not comparison.rankings:
        return []

    entries = {
        entry.pk: entry
        for entry in ReserveEntry.objects.filter(
            pk__in=[entry_id for entry_id, _spread in comparison.volatile]
        ).with_related()
    }
    rows = []
    for entry_id, spread in comparison.volatile:
        entry = entries.get(entry_id)
        if entry is None:
            continue
        rows.append(
            {
                "entry": entry,
                "spread": spread,
                "ranks": [
                    {"scheme": ranking.scheme, "rank": ranking.rank_of(entry_id)}
                    for ranking in comparison.rankings
                ],
            }
        )
    return rows


class StabilityView(AnalyticsBaseMixin, TemplateView):
    """Анализ устойчивости ранжирования к неточности весов."""

    template_name = "analytics/stability.html"
    page_title = _("Устойчивость ранжирования")
    page_subtitle = _("Как меняется порядок кандидатов при возмущении весов критериев")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        form = StabilityForm(self.request.GET or None)
        context["form"] = form

        scheme = None
        deltas: tuple[float, ...] = (0.10, 0.20, 0.30)
        top = 10
        if form.is_bound and form.is_valid():
            scheme = form.cleaned_data.get("scheme")
            deltas = form.deltas()
            top = form.cleaned_data["top"]

        # Область видимости — по той же причине, что и на странице сравнения
        # схем: отчёт называет резервистов поимённо.
        entries = apply_scope_by_path(
            ReserveEntry.objects.in_reserve().with_related(),
            current_user(self.request),
            prefix="employee",
        )[:ANALYSIS_LIMIT]
        try:
            report, case = stability(scheme, entries, deltas=deltas, top=top)
        except AnalyticsError as error:
            context["report"] = None
            context["error"] = str(error)
            return context

        context["report"] = report
        context["case"] = case
        context["scheme"] = scheme or ScoringScheme.objects.default()
        context["rows"] = _stability_rows(report, case)
        context["critical"] = (
            case.criteria[report.critical_criterion]
            if report.critical_criterion is not None and case.criteria
            else None
        )
        return context


def _stability_rows(report, case) -> list[dict[str, Any]]:
    """Сводит результаты возмущений по критериям в строки таблицы."""
    by_criterion: dict[int, list] = {}
    for outcome in report.outcomes:
        if outcome.criterion_index is not None:
            by_criterion.setdefault(outcome.criterion_index, []).append(outcome)

    rows = []
    for index, outcomes in sorted(by_criterion.items()):
        if index >= len(case.criteria):
            continue
        worst = min(outcomes, key=lambda item: item.correlation)
        rows.append(
            {
                "criterion": case.criteria[index],
                "worst_correlation": worst.correlation,
                "worst_delta": worst.delta,
                "stable_share": worst.stable_share,
                "top_agreement": worst.top_agreement,
                "max_shift": max(item.max_rank_shift for item in outcomes),
            }
        )
    return sorted(rows, key=lambda row: row["worst_correlation"])


# ---------------------------------------------------------------------------
# Матрица 9-box
# ---------------------------------------------------------------------------
class NineBoxView(AnalyticsBaseMixin, FilterMixin, TemplateView):
    """Матрица «результативность — потенциал»."""

    template_name = "analytics/ninebox.html"
    page_title = _("Матрица 9-box")
    page_subtitle = _("Результативность и потенциал: распределение и кадровые решения")
    filter_form_class = NineBoxFilterForm

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        # Тип выборки объявлен обобщённо: отбор формы возвращает выборку без собственных методов
        # модели, и без объявления вывод типов принял бы за тип переменной результат первого
        # присваивания.
        queryset: QuerySet[NineBoxPlacement] = apply_scope_by_path(
            NineBoxPlacement.objects.current(), current_user(self.request), prefix="employee"
        )
        form = self.filter_form_class(self.request.GET or None, user=self.request.user)
        if form.is_valid():
            queryset = form.filter(queryset)

        context["filter_form"] = form
        context["grid"] = nine_box_grid(queryset)
        context["has_active_filters"] = bool(
            {key: value for key, value in self.request.GET.items() if value}
        )
        context["thresholds"] = _nine_box_thresholds()
        return context


def _nine_box_thresholds() -> dict[str, int]:
    """Границы зон матрицы из настроек организации."""
    from core.settings_types import get_reservix_settings

    options = get_reservix_settings()
    return {"low": options["NINE_BOX_LOW_MAX"], "mid": options["NINE_BOX_MID_MAX"]}


class PlacementListView(AnalyticsBaseMixin, FilterMixin, WindowedPaginationMixin, ListView):
    """Список размещений в матрице — рабочий инструмент правки."""

    template_name = "analytics/placements.html"
    model = NineBoxPlacement
    context_object_name = "placements"
    paginate_by = 30
    page_title = _("Размещения в матрице")
    page_subtitle = _("Оценки результативности и потенциала с указанием их происхождения")
    filter_form_class = NineBoxFilterForm

    def get_queryset(self):
        queryset = apply_scope_by_path(
            NineBoxPlacement.objects.current().with_related(),
            current_user(self.request),
            prefix="employee",
        )
        form = self.get_filter_form()
        if form is not None and form.is_valid():
            queryset = form.filter(queryset)
        self._filter_form = form
        return queryset.order_by("-performance", "-potential")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        current = NineBoxPlacement.objects.current()
        context["auto_count"] = current.filter(placed_by__isnull=True, is_adjusted=False).count()
        context["adjusted_count"] = current.filter(is_adjusted=True).count()
        return context


class PlacementCreateView(AnalyticsBaseMixin, AuditedFormMixin, CreateView):
    """Размещение сотрудника в матрице вручную."""

    template_name = "analytics/placement_form.html"
    model = NineBoxPlacement
    form_class = PlacementForm
    allowed_roles = VIEWER_ROLES
    page_title = _("Оценка результативности и потенциала")

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        """Сохраняет размещение через сервисный слой."""
        placement = place_employee(
            form.cleaned_data["employee"],
            performance=float(form.cleaned_data["performance"]),
            potential=float(form.cleaned_data["potential"]),
            actor=current_user(self.request),
            comment=form.cleaned_data.get("comment", ""),
            period_start=form.cleaned_data.get("period_start"),
            period_end=form.cleaned_data.get("period_end"),
        )
        self.object = placement
        messages.success(self.request, _("Оценка сохранена"))
        return redirect("analytics:ninebox")


@login_required
def calibrate_view(request: AuthenticatedRequest, pk: int) -> HttpResponse:
    """Корректировка размещения по итогам обсуждения."""
    if request.user.role not in VIEWER_ROLES:
        raise PermissionDenied(_("Действие доступно кадровой службе и руководителям"))

    placement = get_object_or_404(NineBoxPlacement.objects.with_related(), pk=pk)
    session_id = request.GET.get("session") or request.POST.get("session")
    session = CalibrationSession.objects.filter(pk=session_id).first() if session_id else None

    if request.method == "POST":
        form = CalibrationForm(request.POST)
        if form.is_valid():
            try:
                calibrate(
                    placement,
                    performance=float(form.cleaned_data["performance"]),
                    potential=float(form.cleaned_data["potential"]),
                    comment=form.cleaned_data["comment"],
                    actor=request.user,
                    session=session,
                )
            except AnalyticsError as error:
                messages.error(request, str(error))
            else:
                messages.success(request, _("Оценка скорректирована"))
                if session is not None:
                    return redirect(session.get_absolute_url())
                return redirect("analytics:ninebox")
    else:
        form = CalibrationForm(
            initial={
                "performance": placement.performance,
                "potential": placement.potential,
            }
        )

    return render(
        request,
        "analytics/calibrate.html",
        {
            "placement": placement,
            "session": session,
            "form": form,
            "page_title": _("Корректировка оценки"),
            "page_section": "analytics",
        },
    )


@login_required
def auto_place_view(request: AuthenticatedRequest) -> HttpResponse:
    """Предварительное размещение сотрудников по данным системы."""
    _require_methodologist(request)
    if request.method != "POST":
        return redirect("analytics:ninebox")

    employees = list(
        Employee.objects.active()
        .filter(is_external=False)
        .select_related("position", "position__grade")
    )
    summary = auto_place(employees)
    messages.success(
        request,
        _(
            "Размещено предварительно: %(placed)s. Сохранены решения руководителей: "
            "%(skipped)s. Без достаточных данных: %(no_data)s."
        )
        % summary,
    )
    return redirect("analytics:ninebox")


# ---------------------------------------------------------------------------
# Сессии калибровки
# ---------------------------------------------------------------------------
class CalibrationListView(AnalyticsBaseMixin, FilterMixin, WindowedPaginationMixin, ListView):
    """Сессии калибровки: сверка оценок руководителями."""

    template_name = "analytics/calibrations.html"
    model = CalibrationSession
    context_object_name = "sessions"
    paginate_by = 20
    page_title = _("Сессии калибровки")
    page_subtitle = _("Совместная сверка оценок результативности и потенциала")
    filter_form_class = CalibrationFilterForm

    def get_queryset(self):
        # Число рассмотренных и изменённых оценок считается агрегатами.
        queryset = (
            CalibrationSession.objects.select_related("branch", "department", "facilitator")
            .annotate(
                placements_total=Count("placements", distinct=True),
                placements_adjusted=Count(
                    "placements", filter=Q(placements__is_adjusted=True), distinct=True
                ),
            )
            .order_by("-held_on")
        )
        form = self.get_filter_form()
        if form is not None and form.is_valid():
            queryset = form.filter(queryset)
        self._filter_form = form
        return queryset


class CalibrationDetailView(AnalyticsBaseMixin, DetailView):
    """Карточка сессии: рассмотренные размещения и результат обсуждения."""

    template_name = "analytics/calibration_detail.html"
    model = CalibrationSession
    context_object_name = "session"
    slug_field = "public_id"
    slug_url_kwarg = "public_id"
    page_title = _("Сессия калибровки")

    def get_queryset(self):
        return CalibrationSession.objects.select_related("branch", "department", "facilitator")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        session: CalibrationSession = self.object
        context["placements"] = session.placements.select_related(
            "employee", "employee__department"
        ).order_by("-is_adjusted", "employee__last_name")
        context["effect"] = calibration_effect(session)
        context["close_form"] = CalibrationCloseForm()
        context["page_title"] = session.name
        return context


class CalibrationCreateView(AnalyticsBaseMixin, AuditedFormMixin, CreateView):
    """Назначение сессии калибровки."""

    template_name = "analytics/calibration_form.html"
    model = CalibrationSession
    form_class = CalibrationSessionForm
    allowed_roles = METHODOLOGIST_ROLES
    page_title = _("Новая сессия калибровки")

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs


class CalibrationUpdateView(AnalyticsBaseMixin, AuditedFormMixin, UpdateView):
    """Правка сессии калибровки."""

    template_name = "analytics/calibration_form.html"
    model = CalibrationSession
    form_class = CalibrationSessionForm
    allowed_roles = METHODOLOGIST_ROLES
    slug_field = "public_id"
    slug_url_kwarg = "public_id"
    page_title = _("Правка сессии калибровки")

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs


@login_required
def close_calibration_view(request: AuthenticatedRequest, public_id) -> HttpResponse:
    """Завершение сессии калибровки с протоколом принятых решений."""
    _require_methodologist(request)
    session = get_object_or_404(CalibrationSession, public_id=public_id)
    if request.method != "POST":
        return redirect(session.get_absolute_url())

    form = CalibrationCloseForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Протокол сессии обязателен и должен быть содержательным"))
        return redirect(session.get_absolute_url())

    try:
        complete_calibration(session, decisions=form.cleaned_data["decisions"], actor=request.user)
    except AnalyticsError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, _("Сессия завершена, протокол сохранён"))
    return redirect(session.get_absolute_url())


@login_required
def ensure_criteria_view(request: AuthenticatedRequest) -> HttpResponse:
    """Сверка справочника критериев с реализованной методикой."""
    _require_methodologist(request)
    if request.method != "POST":
        return redirect("analytics:criteria")

    criteria = ensure_criteria()
    messages.success(
        request,
        _("Справочник сверен с методикой: критериев — %(count)s") % {"count": len(criteria)},
    )
    return redirect("analytics:criteria")


def _require_methodologist(request: AuthenticatedRequest) -> None:
    """Проверяет право менять методику расчёта."""
    if request.user.role not in METHODOLOGIST_ROLES:
        raise PermissionDenied(_("Изменение методики доступно кадровой службе"))
