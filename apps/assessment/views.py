"""Представления процедур оценки."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, cast

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Avg, Count, Prefetch, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import DetailView, ListView, TemplateView
from django.views.generic.edit import CreateView, UpdateView

from apps.assessment.aggregation import employee_levels
from apps.assessment.forms import (
    AnswerForm,
    CampaignFilterForm,
    CampaignForm,
    ScoreEntryForm,
    SessionFilterForm,
    SessionRejectForm,
    SessionSummaryForm,
    TestFilterForm,
)
from apps.assessment.models import (
    Assessment360Respondent,
    AssessmentCampaign,
    AssessmentMethod,
    AssessmentScore,
    AssessmentSession,
    AttemptStatus,
    CampaignStatus,
    RespondentStatus,
    SessionStatus,
    Test,
    TestAnswer,
    TestAttempt,
    TestQuestion,
)
from apps.assessment.services import (
    AssessmentError,
    aggregate_360,
    approve_session,
    attempt_questions,
    blind_spots,
    campaign_progress,
    check_open_answer,
    close_campaign,
    finish_attempt,
    launch_campaign,
    open_360_session,
    pending_manual_checks,
    pending_sessions_for,
    publish_attempt,
    question_options,
    reject_session,
    save_answer,
    save_scores,
    start_attempt,
    submit_session,
)
from apps.competencies.models import Competency
from apps.people.models import Employee
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
from core.permissions import can_view_employee, current_user, is_privileged

if TYPE_CHECKING:
    from apps.accounts.models import User

#: Роли, ведущие кампании оценки.
ORGANIZER_ROLES: tuple[str, ...] = (Role.ADMIN, Role.HR)

#: Роли, которым может быть назначена сессия оценки.
ASSESSOR_ROLES: tuple[str, ...] = (Role.ADMIN, Role.HR, Role.MANAGER, Role.MENTOR, Role.EMPLOYEE)

#: Роли, которым доступны сводные результаты опроса 360° по сотруднику.
SURVEY_ROLES: tuple[str, ...] = (Role.ADMIN, Role.HR, Role.MANAGER)


class AssessmentBaseMixin(RoleRequiredMixin, PageMetaMixin, BreadcrumbMixin):
    """Общее поведение страниц раздела оценки."""

    allowed_roles = ASSESSOR_ROLES
    page_section = "assessment"
    permission_denied_message = _("Раздел оценки недоступен для вашей роли")
    breadcrumb_root_url = "assessment:inbox"

    def get_breadcrumbs(self, trail):
        root = (
            "assessment:list"
            if self.request.user.role in ORGANIZER_ROLES
            else self.breadcrumb_root_url
        )
        trail.add(_("Оценка"), root)
        title = getattr(self, "page_title", "")
        if title:
            trail.add(str(title))
        return trail

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_section_label"] = _("Оценка компетенций")
        context["is_organizer"] = self.request.user.role in ORGANIZER_ROLES
        return context


# ---------------------------------------------------------------------------
# Кампании
# ---------------------------------------------------------------------------
class CampaignListView(AssessmentBaseMixin, FilterMixin, WindowedPaginationMixin, ListView):
    """Кампании оценки."""

    template_name = "assessment/campaigns.html"
    model = AssessmentCampaign
    context_object_name = "campaigns"
    allowed_roles = ORGANIZER_ROLES
    paginate_by = 20
    page_title = _("Кампании оценки")
    page_subtitle = _("Процедуры оценки: период, охват, ход выполнения")
    filter_form_class = CampaignFilterForm

    def get_queryset(self):
        return self.apply_filters(
            AssessmentCampaign.objects.select_related("competency_model", "branch", "department")
            .annotate(
                session_count=Count("sessions", distinct=True),
                done_count=Count(
                    "sessions",
                    filter=Q(
                        sessions__status__in=[
                            SessionStatus.SUBMITTED,
                            SessionStatus.APPROVED,
                        ]
                    ),
                    distinct=True,
                ),
            )
            .order_by("-period_start", "name")
        )

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["active_count"] = AssessmentCampaign.objects.filter(
            status=CampaignStatus.ACTIVE
        ).count()
        context["overdue"] = AssessmentCampaign.objects.overdue().count()
        context["pending_approval"] = AssessmentSession.objects.filter(
            status=SessionStatus.SUBMITTED
        ).count()
        return context


class CampaignDetailView(AssessmentBaseMixin, DetailView):
    """Карточка кампании: ход выполнения, состав сессий, действия организатора."""

    template_name = "assessment/campaign_detail.html"
    model = AssessmentCampaign
    context_object_name = "campaign"
    slug_field = "public_id"
    slug_url_kwarg = "public_id"
    allowed_roles = ORGANIZER_ROLES

    def get_queryset(self):
        return AssessmentCampaign.objects.select_related(
            "competency_model", "branch", "department", "created_by"
        )

    def get_page_title(self) -> str:
        return self.object.name

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        campaign = self.object

        context["page_subtitle"] = _("%(start)s — %(end)s, срок %(deadline)s") % {
            "start": campaign.period_start.strftime("%d.%m.%Y"),
            "end": campaign.period_end.strftime("%d.%m.%Y"),
            "deadline": campaign.deadline.strftime("%d.%m.%Y"),
        }
        context["progress"] = campaign_progress(campaign)
        context["can_launch"] = campaign.status in {
            CampaignStatus.DRAFT,
            CampaignStatus.ACTIVE,
        }
        context["can_close"] = campaign.status == CampaignStatus.ACTIVE

        context["pending"] = (
            campaign.sessions.filter(status=SessionStatus.SUBMITTED)
            .select_related("employee", "assessor")
            .order_by("submitted_at")[:20]
        )
        context["laggards"] = self._laggards(campaign)
        context["by_method"] = (
            campaign.sessions.values("method")
            .annotate(total=Count("id"), mean=Avg("scores__level"))
            .order_by("method")
        )
        context["method_labels"] = dict(AssessmentMethod.choices)
        context["respondent_stats"] = (
            campaign.respondents.values("status").annotate(total=Count("id")).order_by()
        )
        context["respondent_labels"] = dict(RespondentStatus.choices)
        return context

    @staticmethod
    def _laggards(campaign: AssessmentCampaign):
        """Оценивающие с наибольшим числом незаполненных сессий."""
        return (
            AssessmentSession.objects.filter(
                campaign=campaign,
                status__in=[SessionStatus.PLANNED, SessionStatus.IN_PROGRESS],
                assessor__isnull=False,
            )
            .values("assessor__last_name", "assessor__first_name", "assessor_id")
            .annotate(total=Count("id"))
            .order_by("-total")[:10]
        )


class CampaignCreateView(AssessmentBaseMixin, AuditedFormMixin, CreateView):
    """Создание кампании оценки."""

    template_name = "assessment/campaign_form.html"
    model = AssessmentCampaign
    form_class = CampaignForm
    allowed_roles = ORGANIZER_ROLES
    page_title = _("Новая кампания оценки")
    page_subtitle = _("Период, охват и состав методов оценки")

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(
            self.request,
            _("Кампания создана. Запустите её, чтобы сформировать сессии оценки."),
        )
        return response


class CampaignUpdateView(AssessmentBaseMixin, AuditedFormMixin, UpdateView):
    """Изменение кампании оценки."""

    template_name = "assessment/campaign_form.html"
    model = AssessmentCampaign
    form_class = CampaignForm
    slug_field = "public_id"
    slug_url_kwarg = "public_id"
    allowed_roles = ORGANIZER_ROLES
    page_title = _("Изменение кампании")

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, _("Изменения сохранены"))
        return response


@login_required
def launch_campaign_view(request: AuthenticatedRequest, public_id) -> HttpResponse:
    """Запускает кампанию: формирует сессии оценки и приглашения 360°."""
    _require_organizer(request)
    campaign = get_object_or_404(AssessmentCampaign, public_id=public_id)

    if request.method != "POST":
        return redirect(campaign.get_absolute_url())

    try:
        result = launch_campaign(campaign, actor=request.user)
    except AssessmentError as error:
        messages.error(request, str(error))
    else:
        messages.success(
            request,
            _(
                "Кампания запущена: участников %(participants)s, "
                "создано сессий %(sessions)s, приглашений 360° — %(invitations)s"
            )
            % result,
        )
    return redirect(campaign.get_absolute_url())


@login_required
def close_campaign_view(request: AuthenticatedRequest, public_id) -> HttpResponse:
    """Завершает кампанию, отменяя незаполненные сессии."""
    _require_organizer(request)
    campaign = get_object_or_404(AssessmentCampaign, public_id=public_id)

    if request.method != "POST":
        return redirect(campaign.get_absolute_url())

    result = close_campaign(campaign, actor=request.user)
    messages.success(
        request,
        _("Кампания завершена. Отменено незаполненных сессий: %(cancelled_sessions)s") % result,
    )
    return redirect(campaign.get_absolute_url())


def _require_organizer(request: AuthenticatedRequest) -> None:
    """Требует роли организатора кампаний."""
    if not request.user.is_authenticated or request.user.role not in ORGANIZER_ROLES:
        raise PermissionDenied(_("Действие доступно кадровой службе"))


# ---------------------------------------------------------------------------
# Работа оценивающего
# ---------------------------------------------------------------------------
class SessionInboxView(AssessmentBaseMixin, WindowedPaginationMixin, ListView):
    """Сессии, ожидающие заполнения текущим пользователем."""

    template_name = "assessment/inbox.html"
    context_object_name = "sessions"
    paginate_by = 25
    page_title = _("Мои задачи по оценке")
    page_subtitle = _("Сессии оценки, ожидающие заполнения")

    def get_queryset(self):
        return pending_sessions_for(current_user(self.request))

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        user = current_user(self.request)

        context["submitted"] = (
            AssessmentSession.objects.filter(assessor=user, status=SessionStatus.SUBMITTED)
            .select_related("employee", "campaign")
            .order_by("-submitted_at")[:10]
        )
        context["rejected"] = (
            AssessmentSession.objects.filter(assessor=user, status=SessionStatus.REJECTED)
            .select_related("employee", "campaign")
            .order_by("-updated_at")[:10]
        )
        context["invitations"] = (
            Assessment360Respondent.objects.filter(
                respondent=user,
                status__in=[RespondentStatus.INVITED, RespondentStatus.IN_PROGRESS],
                campaign__status=CampaignStatus.ACTIVE,
            )
            .select_related("employee", "campaign")
            .order_by("campaign__deadline")
        )
        return context


class SessionDetailView(AssessmentBaseMixin, DetailView):
    """Просмотр внесённой сессии оценки."""

    template_name = "assessment/session_detail.html"
    model = AssessmentSession
    context_object_name = "session"
    slug_field = "public_id"
    slug_url_kwarg = "public_id"

    def get_queryset(self):
        return AssessmentSession.objects.select_related(
            "employee", "assessor", "campaign", "competency_model", "approved_by"
        ).prefetch_related(
            Prefetch("scores", queryset=AssessmentScore.objects.select_related("competency"))
        )

    def get_object(self, queryset=None):
        session = super().get_object(queryset)
        if not _can_view_session(current_user(self.request), session):
            raise PermissionDenied(_("Сессия оценки недоступна"))
        return session

    def get_page_title(self) -> str:
        return _("Оценка: %(employee)s") % {"employee": self.object.employee.short_name}

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        session = self.object
        requirements = {
            requirement.competency_id: requirement
            for requirement in session.competency_model.requirements.all()
        }

        rows = []
        for score in session.scores.all():
            requirement = requirements.get(score.competency_id)
            rows.append(
                {
                    "score": score,
                    "competency": score.competency,
                    "target": requirement.target_level if requirement else None,
                    "delta": (
                        round(float(score.level) - requirement.target_level, 1)
                        if requirement
                        else None
                    ),
                }
            )
        context["rows"] = rows
        context["can_approve"] = (
            self.request.user.role in ORGANIZER_ROLES and session.status == SessionStatus.SUBMITTED
        )
        context["reject_form"] = SessionRejectForm()
        context["can_edit"] = session.is_editable and session.assessor_id == self.request.user.pk
        return context


@login_required
def fill_session_view(request: AuthenticatedRequest, public_id) -> HttpResponse:
    """Форма выставления баллов по сессии оценки."""
    session = get_object_or_404(
        AssessmentSession.objects.select_related("employee", "campaign", "competency_model"),
        public_id=public_id,
    )

    if session.assessor_id != request.user.pk and not is_privileged(current_user(request)):
        raise PermissionDenied(_("Сессия оценки назначена другому пользователю"))
    if not session.is_editable:
        messages.info(request, _("Сессия уже внесена и недоступна для изменений"))
        return redirect(session.get_absolute_url())

    requirements = list(
        session.competency_model.requirements.with_competency().prefetch_related(
            "competency__scale_levels"
        )
    )
    existing = {score.competency_id: score for score in session.scores.select_related("competency")}

    if request.method == "POST":
        form = ScoreEntryForm(request.POST, requirements=requirements)
        summary_form = SessionSummaryForm(request.POST, instance=session)

        if form.is_valid() and summary_form.is_valid():
            summary_form.save()
            saved = save_scores(session, form.levels, comments=form.comments)

            if "submit" in request.POST:
                try:
                    submit_session(session)
                except AssessmentError as error:
                    messages.error(request, str(error))
                else:
                    messages.success(request, _("Оценка внесена и передана на утверждение"))
                    return redirect(session.get_absolute_url())
            else:
                messages.success(
                    request,
                    _("Сохранено оценок: %(count)s. Работу можно продолжить позже.")
                    % {"count": saved},
                )
                return redirect("assessment:session_fill", public_id=session.public_id)
    else:
        initial = {}
        for requirement in requirements:
            score = existing.get(requirement.competency_id)
            if score is not None:
                initial[f"level_{requirement.competency_id}"] = f"{float(score.level):.1f}"
                initial[f"comment_{requirement.competency_id}"] = score.comment
        form = ScoreEntryForm(initial=initial, requirements=requirements)
        summary_form = SessionSummaryForm(instance=session)

    from core.breadcrumbs import BreadcrumbTrail

    trail = BreadcrumbTrail(request)
    trail.add(_("Оценка"), "assessment:inbox")
    trail.add(session.employee.short_name)

    return render(
        request,
        "assessment/session_fill.html",
        {
            "session": session,
            "form": form,
            "summary_form": summary_form,
            "rows": form.rows(),
            "page_title": _("Оценка: %(employee)s") % {"employee": session.employee.short_name},
            "page_subtitle": session.competency_model.name,
            "page_section": "assessment",
            "page_section_label": _("Оценка компетенций"),
            "breadcrumbs": trail.resolve(),
            "scale_hint": _(
                "Рядом с каждым уровнем указаны поведенческие индикаторы. "
                "Оценивайте наблюдаемое поведение, а не отношение к сотруднику."
            ),
        },
    )


@login_required
def approve_session_view(request: AuthenticatedRequest, public_id) -> HttpResponse:
    """Утверждает внесённую сессию оценки."""
    _require_organizer(request)
    session = get_object_or_404(AssessmentSession, public_id=public_id)

    if request.method != "POST":
        return redirect(session.get_absolute_url())

    if session.status != SessionStatus.SUBMITTED:
        messages.error(request, _("Утвердить можно только внесённую сессию"))
    else:
        approve_session(session, actor=current_user(request))
        messages.success(request, _("Оценка утверждена и учитывается в расчётах"))

    return redirect(session.get_absolute_url())


@login_required
def reject_session_view(request: AuthenticatedRequest, public_id) -> HttpResponse:
    """Возвращает сессию оценивающему с указанием причины."""
    _require_organizer(request)
    session = get_object_or_404(AssessmentSession, public_id=public_id)

    if request.method != "POST":
        return redirect(session.get_absolute_url())

    form = SessionRejectForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Укажите причину возврата"))
    elif session.status != SessionStatus.SUBMITTED:
        messages.error(request, _("Вернуть можно только внесённую сессию"))
    else:
        reject_session(session, actor=current_user(request), reason=form.cleaned_data["reason"])
        messages.success(request, _("Сессия возвращена оценивающему"))

    return redirect(session.get_absolute_url())


def _can_view_session(user: User, session: AssessmentSession) -> bool:
    """Право просмотра сессии оценки."""
    if session.assessor_id == user.pk:
        return True
    if is_privileged(user):
        return can_view_employee(user, session.employee)
    if session.method == AssessmentMethod.ASSESSMENT_360:
        return False
    if session.employee.user_id == user.pk:
        return True
    return can_view_employee(user, session.employee)


# ---------------------------------------------------------------------------
# Результаты оцениваемого
# ---------------------------------------------------------------------------
class MyResultsView(AssessmentBaseMixin, TemplateView):
    """Результаты оценки текущего пользователя."""

    template_name = "assessment/my_results.html"
    page_title = _("Мои результаты оценки")
    page_subtitle = _("Сведённые уровни владения и обратная связь по методу 360°")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        employee = getattr(self.request.user, "employee_card", None)
        context["employee"] = employee

        if employee is None:
            return context

        levels = employee_levels(employee.pk)
        competencies = {
            competency.pk: competency
            for competency in Competency.objects.filter(pk__in=levels).select_related("category")
        }
        rows: list[dict[str, Any]] = [
            {
                "competency": competencies[competency_id],
                "level": measured.level,
                "confidence": measured.confidence,
                "methods": measured.method_count,
                "self_bias": measured.self_bias,
                "latest": measured.latest_date,
                "observations": measured.observations,
            }
            for competency_id, measured in levels.items()
            if competency_id in competencies
        ]
        rows.sort(key=lambda row: (row["competency"].category.sort_order, -row["level"]))
        context["rows"] = rows

        context["sessions"] = (
            AssessmentSession.objects.filter(employee=employee)
            .exclude(method=AssessmentMethod.ASSESSMENT_360)
            .select_related("campaign", "assessor")
            .order_by("-assessment_date")[:12]
        )
        context["attempts"] = (
            TestAttempt.objects.filter(employee=employee)
            .select_related("test")
            .order_by("-started_at")[:10]
        )
        context["surveys"] = self._surveys(employee)
        return context

    def _surveys(self, employee: Employee) -> list[dict]:
        """Результаты опросов 360°, в которых участвовал сотрудник."""
        campaigns = AssessmentCampaign.objects.filter(respondents__employee=employee).distinct()

        result = []
        for campaign in campaigns:
            aggregates = aggregate_360(campaign, employee)
            if not aggregates:
                continue
            competency_ids = {
                competency_id for item in aggregates for competency_id in item.by_competency
            }
            competencies = {
                competency.pk: competency
                for competency in Competency.objects.filter(pk__in=competency_ids)
            }
            result.append(
                {
                    "campaign": campaign,
                    "aggregates": aggregates,
                    "blind_spots": [
                        {**item, "competency": competencies.get(item["competency_id"])}
                        for item in blind_spots(aggregates)[:5]
                    ],
                }
            )
        return result


class Survey360View(AssessmentBaseMixin, TemplateView):
    """Результаты опроса 360° по конкретному сотруднику."""

    template_name = "assessment/survey_360.html"
    allowed_roles = SURVEY_ROLES
    page_title = _("Оценка 360°")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        campaign = get_object_or_404(AssessmentCampaign, public_id=self.kwargs["public_id"])
        employee = get_object_or_404(
            Employee.objects.select_related("position", "department"),
            public_id=self.kwargs["employee_id"],
        )

        if not can_view_employee(current_user(self.request), employee):
            raise PermissionDenied(_("Карточка сотрудника недоступна"))

        aggregates = aggregate_360(campaign, employee)
        spots = blind_spots(aggregates)
        competency_ids = {item["competency_id"] for item in spots} | {
            competency_id for item in aggregates for competency_id in item.by_competency
        }
        competencies = {
            competency.pk: competency
            for competency in Competency.objects.filter(pk__in=competency_ids)
        }

        context["campaign"] = campaign
        context["employee"] = employee
        context["page_title"] = _("Оценка 360°: %(name)s") % {"name": employee.short_name}
        context["aggregates"] = aggregates
        context["competencies"] = competencies
        context["blind_spots"] = [
            {**item, "competency": competencies.get(item["competency_id"])} for item in spots
        ]
        context["disclosure_threshold"] = Assessment360Respondent.MIN_RESPONSES_FOR_DISCLOSURE
        return context


@login_required
def respond_360_view(request: AuthenticatedRequest, token: str) -> HttpResponse:
    """Форма респондента опроса 360°."""
    if not request.user.is_authenticated:
        raise PermissionDenied(_("Для заполнения анкеты требуется вход в систему"))

    invitation = get_object_or_404(
        Assessment360Respondent.objects.select_related("campaign", "employee"),
        access_token=token,
    )

    if invitation.respondent_id != request.user.pk:
        raise PermissionDenied(_("Приглашение адресовано другому пользователю"))
    if invitation.status == RespondentStatus.COMPLETED:
        messages.info(request, _("Анкета уже заполнена"))
        return redirect("assessment:inbox")
    if invitation.campaign.status != CampaignStatus.ACTIVE:
        messages.error(request, _("Опрос завершён"))
        return redirect("assessment:inbox")

    session = open_360_session(invitation)
    return redirect("assessment:session_fill", public_id=session.public_id)


@login_required
def decline_360_view(request: AuthenticatedRequest, token: str) -> HttpResponse:
    """Отказ от участия в опросе 360°."""
    if not request.user.is_authenticated:
        raise PermissionDenied(_("Требуется вход в систему"))

    invitation = get_object_or_404(Assessment360Respondent, access_token=token)
    if invitation.respondent_id != request.user.pk:
        raise PermissionDenied(_("Приглашение адресовано другому пользователю"))

    if request.method == "POST":
        invitation.status = RespondentStatus.DECLINED
        invitation.decline_reason = request.POST.get("reason", "")[:255]
        invitation.save(update_fields=["status", "decline_reason", "updated_at"])
        messages.success(request, _("Отказ зафиксирован"))

    return redirect("assessment:inbox")


# ---------------------------------------------------------------------------
# Тестирование
# ---------------------------------------------------------------------------
class TestListView(AssessmentBaseMixin, FilterMixin, ListView):
    """Доступные тесты с историей собственных попыток."""

    template_name = "assessment/tests.html"
    model = Test
    context_object_name = "tests"
    page_title = _("Тестирование")
    page_subtitle = _("Проверка профессиональных компетенций с автоматическим подсчётом")
    filter_form_class = TestFilterForm

    def get_queryset(self):
        return self.apply_filters(
            Test.objects.filter(is_active=True)
            .select_related("competency")
            .annotate(question_total=Count("questions", filter=Q(questions__is_active=True)))
            .order_by("competency__name", "name")
        )

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        employee = getattr(self.request.user, "employee_card", None)
        context["employee"] = employee

        attempts: dict[int, list[TestAttempt]] = {}
        if employee is not None:
            for attempt in TestAttempt.objects.filter(employee=employee).order_by("-started_at"):
                attempts.setdefault(attempt.test_id, []).append(attempt)
        context["attempts"] = attempts

        user = current_user(self.request)
        if user.role in ORGANIZER_ROLES:
            context["pending_checks"] = pending_manual_checks(user)[:10]
        return context


class TestDetailView(AssessmentBaseMixin, DetailView):
    """Карточка теста: условия прохождения и история попыток."""

    template_name = "assessment/test_detail.html"
    model = Test
    context_object_name = "test"

    def get_queryset(self):
        return Test.objects.select_related("competency").annotate(
            question_total=Count("questions", filter=Q(questions__is_active=True))
        )

    def get_page_title(self) -> str:
        return self.object.name

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        employee = getattr(self.request.user, "employee_card", None)
        context["employee"] = employee
        context["page_subtitle"] = self.object.competency.name

        if employee is not None:
            attempts = TestAttempt.objects.filter(test=self.object, employee=employee).order_by(
                "-started_at"
            )
            context["attempts"] = attempts
            used = attempts.exclude(status=AttemptStatus.CANCELLED).count()
            context["attempts_left"] = max(0, self.object.max_attempts - used)
            context["can_start"] = self.object.is_ready and context["attempts_left"] > 0
            context["best"] = attempts.filter(score__isnull=False).order_by("-score").first()
        return context


@login_required
def start_attempt_view(request: AuthenticatedRequest, pk: int) -> HttpResponse:
    """Начинает попытку прохождения теста."""
    test = get_object_or_404(Test, pk=pk, is_active=True)
    employee = getattr(request.user, "employee_card", None)

    if employee is None:
        messages.error(request, _("Тестирование доступно сотрудникам с карточкой в системе"))
        return redirect("assessment:tests")
    if request.method != "POST":
        return redirect("assessment:test_detail", pk=test.pk)

    try:
        attempt = start_attempt(test, employee)
    except AssessmentError as error:
        messages.error(request, str(error))
        return redirect("assessment:test_detail", pk=test.pk)

    return redirect("assessment:attempt", public_id=attempt.public_id)


@login_required
def take_attempt_view(request: AuthenticatedRequest, public_id) -> HttpResponse:
    """Прохождение теста."""
    attempt = get_object_or_404(
        TestAttempt.objects.select_related("test", "employee", "test__competency"),
        public_id=public_id,
    )

    if attempt.employee.user_id != request.user.pk:
        raise PermissionDenied(_("Попытка принадлежит другому сотруднику"))

    if attempt.status != AttemptStatus.IN_PROGRESS:
        return redirect("assessment:attempt_result", public_id=attempt.public_id)

    if attempt.is_expired:
        finish_attempt(attempt, expired=True)
        messages.warning(request, _("Время выполнения теста истекло"))
        return redirect("assessment:attempt_result", public_id=attempt.public_id)

    questions = attempt_questions(attempt)
    saved_answers = {
        answer.question_id: answer
        for answer in attempt.answers.prefetch_related("selected_options")
    }

    forms = []
    for index, question in enumerate(questions, start=1):
        options = question_options(question, attempt)
        prefix = f"q{question.pk}"

        if request.method == "POST":
            form = AnswerForm(request.POST, prefix=prefix, question=question, options=options)
        else:
            answer = saved_answers.get(question.pk)
            initial: dict[str, Any] = {}
            if answer is not None:
                selected = [option.pk for option in answer.selected_options.all()]
                if question.kind == "multiple":
                    initial["answer"] = selected
                elif question.kind == "open":
                    initial["answer"] = answer.text_answer
                elif selected:
                    initial["answer"] = selected[0]
            form = AnswerForm(initial=initial, prefix=prefix, question=question, options=options)

        forms.append({"number": index, "question": question, "form": form})

    if request.method == "POST" and all(item["form"].is_valid() for item in forms):
        for item in forms:
            # Значения словаря строки формы разнородны, поэтому тип каждого указывается явно: без
            # этого обращение к полям формы ответа выполняется над объектом неизвестного типа.
            answered = cast(TestQuestion, item["question"])
            answer_form = cast(AnswerForm, item["form"])
            try:
                save_answer(
                    attempt,
                    answered,
                    option_ids=answer_form.selected_option_ids,
                    text=answer_form.text_answer,
                )
            except AssessmentError as error:
                messages.error(request, str(error))
                return redirect("assessment:attempt_result", public_id=attempt.public_id)

        if "finish" in request.POST:
            finish_attempt(attempt)
            messages.success(request, _("Попытка завершена, результат подсчитан"))
            return redirect("assessment:attempt_result", public_id=attempt.public_id)

        messages.success(request, _("Ответы сохранены"))
        return redirect("assessment:attempt", public_id=attempt.public_id)

    from core.breadcrumbs import BreadcrumbTrail

    trail = BreadcrumbTrail(request)
    trail.add(_("Оценка"), "assessment:inbox")
    trail.add(_("Тестирование"), "assessment:tests")
    trail.add(attempt.test.name)

    return render(
        request,
        "assessment/attempt.html",
        {
            "attempt": attempt,
            "forms": forms,
            "answered": len(saved_answers),
            "total": len(questions),
            "page_title": attempt.test.name,
            "page_subtitle": _("Попытка №%(number)s") % {"number": attempt.attempt_number},
            "page_section": "assessment",
            "page_section_label": _("Оценка компетенций"),
            "breadcrumbs": trail.resolve(),
        },
    )


class AttemptResultView(AssessmentBaseMixin, DetailView):
    """Результат попытки с разбором ответов."""

    template_name = "assessment/attempt_result.html"
    model = TestAttempt
    context_object_name = "attempt"
    slug_field = "public_id"
    slug_url_kwarg = "public_id"

    def get_queryset(self):
        return TestAttempt.objects.select_related("test", "employee", "test__competency")

    def get_object(self, queryset=None):
        attempt = super().get_object(queryset)
        user = current_user(self.request)
        own = attempt.employee.user_id == user.pk
        if not own and not (is_privileged(user) and can_view_employee(user, attempt.employee)):
            raise PermissionDenied(_("Результат принадлежит другому сотруднику"))
        return attempt

    def get_page_title(self) -> str:
        return _("Результат: %(test)s") % {"test": self.object.test.name}

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        attempt = self.object

        answers = list(
            attempt.answers.select_related("question")
            .prefetch_related("selected_options", "question__options")
            .order_by("question__sort_order")
        )
        context["answers"] = [
            {
                "answer": answer,
                "question": answer.question,
                "selected": list(answer.selected_options.all()),
                "correct": [
                    option for option in answer.question.options.all() if option.is_correct
                ],
            }
            for answer in answers
        ]
        context["correct_count"] = sum(1 for answer in answers if answer.is_correct)
        context["total"] = len(answers)
        context["published"] = attempt.session_id is not None
        context["own"] = attempt.employee.user_id == self.request.user.pk
        context["can_check"] = (
            attempt.status in {AttemptStatus.CHECKED, AttemptStatus.EXPIRED}
            and self.request.user.role in ORGANIZER_ROLES
        )
        context["can_publish"] = (
            attempt.status in {AttemptStatus.CHECKED, AttemptStatus.EXPIRED}
            and attempt.session_id is None
            and self.request.user.role in ORGANIZER_ROLES
        )
        return context


@login_required
def finish_attempt_view(request: AuthenticatedRequest, public_id) -> HttpResponse:
    """Завершает попытку досрочно."""
    attempt = get_object_or_404(TestAttempt.objects.select_related("employee"), public_id=public_id)

    if attempt.employee.user_id != request.user.pk:
        raise PermissionDenied(_("Попытка принадлежит другому сотруднику"))
    if request.method != "POST":
        return redirect("assessment:attempt", public_id=attempt.public_id)

    finish_attempt(attempt)
    messages.success(request, _("Попытка завершена"))
    return redirect("assessment:attempt_result", public_id=attempt.public_id)


@login_required
def check_answer_view(request: AuthenticatedRequest, pk: int) -> HttpResponse:
    """Экспертная проверка развёрнутого ответа."""
    _require_organizer(request)
    answer = get_object_or_404(
        TestAnswer.objects.select_related("attempt__employee", "attempt__test", "question"), pk=pk
    )
    attempt = answer.attempt
    if not can_view_employee(current_user(request), attempt.employee):
        raise PermissionDenied(_("Сотрудник вне области видимости"))
    if request.method != "POST":
        return redirect("assessment:attempt_result", public_id=attempt.public_id)

    try:
        score = Decimal(request.POST.get("score", "").replace(",", "."))
        check_open_answer(answer, score=score, actor=current_user(request))
    except InvalidOperation:
        messages.error(request, _("Укажите балл числом"))
    except AssessmentError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, _("Ответ проверен, результат попытки пересчитан"))

    url = reverse("assessment:attempt_result", kwargs={"public_id": attempt.public_id})
    return redirect(f"{url}#answer-{answer.pk}")


@login_required
def publish_attempt_view(request: AuthenticatedRequest, public_id) -> HttpResponse:
    """Переносит результат теста в оценку компетенции."""
    _require_organizer(request)
    attempt = get_object_or_404(
        TestAttempt.objects.select_related("test", "employee"), public_id=public_id
    )

    if request.method != "POST":
        return redirect("assessment:attempt_result", public_id=attempt.public_id)

    try:
        publish_attempt(attempt)
    except AssessmentError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, _("Результат теста учтён как оценка компетенции"))

    return redirect("assessment:attempt_result", public_id=attempt.public_id)


# ---------------------------------------------------------------------------
# Сессии кампании — список для организатора
# ---------------------------------------------------------------------------
class CampaignSessionsView(AssessmentBaseMixin, FilterMixin, WindowedPaginationMixin, ListView):
    """Полный список сессий кампании с отбором по состоянию и методу."""

    template_name = "assessment/sessions.html"
    model = AssessmentSession
    context_object_name = "sessions"
    allowed_roles = ORGANIZER_ROLES
    paginate_by = 40
    page_title = _("Сессии оценки")
    filter_form_class = SessionFilterForm

    def get_queryset(self):
        queryset = (
            AssessmentSession.objects.select_related(
                "employee", "assessor", "campaign", "competency_model"
            )
            .annotate(score_count=Count("scores"))
            .order_by("-assessment_date", "employee__last_name")
        )
        campaign_id = self.kwargs.get("public_id")
        if campaign_id:
            queryset = queryset.filter(campaign__public_id=campaign_id)

        form = self.get_filter_form()
        if form is not None and form.is_valid():
            queryset = form.filter(queryset)
        self._filter_form = form
        return queryset

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        campaign_id = self.kwargs.get("public_id")
        if campaign_id:
            context["campaign"] = get_object_or_404(AssessmentCampaign, public_id=campaign_id)
            context["page_subtitle"] = context["campaign"].name
        return context
