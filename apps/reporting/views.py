"""Представления раздела отчётности."""

from __future__ import annotations

import mimetypes
from typing import Any
from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext_lazy as _
from django.views.generic import DetailView, ListView, TemplateView

from apps.reporting.forms import ReportFilterForm, ReportRunForm
from apps.reporting.models import GeneratedReport, ReportCategory, ReportStatus, ReportTemplate
from apps.reporting.renderers import pdf_font_available
from apps.reporting.services import (
    FORMATS_REQUIRING_FONT,
    REPORTING_ROLES,
    ReportError,
    available_templates,
    content_type_for,
    reports_for,
    run,
    templates_by_category,
)
from core.breadcrumbs import BreadcrumbTrail
from core.constants import PRIVILEGED_ROLES, Role
from core.mixins import (
    AuthenticatedRequest,
    BreadcrumbMixin,
    FilterMixin,
    PageMetaMixin,
    RoleRequiredMixin,
    WindowedPaginationMixin,
)
from core.permissions import current_user

#: Разделы каталога, показываемые руководителю на его странице отчётов.
MANAGER_CATEGORIES: tuple[str, ...] = (
    ReportCategory.PERSONNEL,
    ReportCategory.RESERVE,
    ReportCategory.DEVELOPMENT,
)

#: Разделы каталога, показываемые наставнику.
MENTOR_CATEGORIES: tuple[str, ...] = (ReportCategory.DEVELOPMENT,)


class ReportingSectionMixin(RoleRequiredMixin, PageMetaMixin, BreadcrumbMixin):
    """Общее поведение страниц отчётности."""

    allowed_roles = REPORTING_ROLES
    page_section = "reporting"
    permission_denied_message = _("Раздел отчётности недоступен для вашей роли")

    def get_breadcrumbs(self, trail: BreadcrumbTrail) -> BreadcrumbTrail:
        trail.add(_("Отчёты"), "reporting:index")
        title = getattr(self, "page_title", "")
        if title and self.request.resolver_match.url_name != "index":
            trail.add(str(title))
        return trail

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_section_label"] = _("Отчётность")
        return context


class ReportIndexView(ReportingSectionMixin, TemplateView):
    """Каталог отчётов."""

    template_name = "reporting/index.html"
    page_title = _("Отчёты и выгрузки")
    page_subtitle = _("Формирование документов в форматах .docx, .xlsx, .pdf и .csv")

    #: Разделы каталога, показываемые на странице. Пустой набор — все.
    categories: tuple[str, ...] = ()

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)

        catalog = templates_by_category(current_user(self.request))
        if self.categories:
            labels = dict(ReportCategory.choices)
            allowed = {str(labels[key]) for key in self.categories}
            catalog = [item for item in catalog if item[0] in allowed]

        context["catalog"] = catalog
        context["recent"] = reports_for(current_user(self.request)).order_by("-created_at")[:8]
        context["pdf_available"] = pdf_font_available()
        return context


class UnitReportsView(ReportIndexView):
    """Отчёты по подразделению — рабочее место руководителя."""

    allowed_roles = (Role.MANAGER, Role.ADMIN, Role.HR)
    categories = MANAGER_CATEGORIES
    page_title = _("Отчёты по подразделению")
    page_subtitle = _("Состав, участие в резерве и развитие сотрудников подразделения")


class MentorReportsView(ReportIndexView):
    """Отчёты наставника: ход развития закреплённых подопечных."""

    allowed_roles = (Role.MENTOR, Role.ADMIN, Role.HR)
    categories = MENTOR_CATEGORIES
    page_title = _("Отчёты наставника")
    page_subtitle = _("Ход выполнения планов развития подопечных")


class ReportTemplateDetailView(ReportingSectionMixin, DetailView):
    """Карточка отчёта: назначение, состав, параметры и форма запуска."""

    template_name = "reporting/template_detail.html"
    model = ReportTemplate
    context_object_name = "report_template"
    page_title = _("Отчёт")

    def get_queryset(self):
        return available_templates(current_user(self.request))

    def get_page_title(self) -> str:
        return self.object.name

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        template: ReportTemplate = self.object
        context.setdefault("form", ReportRunForm(schema=template.parameters_schema))
        context["previous"] = (
            reports_for(current_user(self.request))
            .filter(template=template)
            .order_by("-created_at")[:5]
        )
        context["needs_font"] = template.output_format in FORMATS_REQUIRING_FONT
        context["pdf_available"] = pdf_font_available()
        return context

    def post(self, request: AuthenticatedRequest, *args, **kwargs) -> HttpResponse:
        self.object = self.get_object()
        template: ReportTemplate = self.object

        form = ReportRunForm(request.POST, schema=template.parameters_schema)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))

        try:
            report = run(template, form.parameters, request.user)
        except ReportError as error:
            messages.error(request, str(error))
            return self.render_to_response(self.get_context_data(form=form))

        if report.status == ReportStatus.QUEUED:
            messages.success(
                request,
                _("Отчёт поставлен в очередь. Он появится в списке выгрузок по готовности."),
            )
        else:
            messages.success(
                request,
                _("Отчёт сформирован: строк — %(rows)s") % {"rows": report.row_count},
            )
        return redirect(report.get_absolute_url())


class GeneratedReportListView(
    ReportingSectionMixin,
    FilterMixin,
    WindowedPaginationMixin,
    ListView,
):
    """История выгрузок."""

    template_name = "reporting/history.html"
    model = GeneratedReport
    context_object_name = "reports"
    paginate_by = 25
    filter_form_class = ReportFilterForm
    page_title = _("История выгрузок")
    page_subtitle = _("Сформированные документы, параметры запуска и авторы")

    def get_queryset(self):
        queryset = reports_for(current_user(self.request)).order_by("-created_at")
        form = self.get_filter_form()
        if form is not None and form.is_valid():
            queryset = form.filter(queryset)
        self._filter_form = form
        return queryset


class GeneratedReportDetailView(ReportingSectionMixin, DetailView):
    """Карточка выгрузки: состояние, параметры запуска, ссылка на файл."""

    template_name = "reporting/report_detail.html"
    model = GeneratedReport
    context_object_name = "report"
    slug_field = "public_id"
    slug_url_kwarg = "public_id"
    page_title = _("Выгрузка")

    def get_queryset(self):
        return reports_for(current_user(self.request))

    def get_page_title(self) -> str:
        return self.object.title

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        report: GeneratedReport = self.object
        # Параметры хранятся кодами; для чтения нужны подписи из описания шаблона — иначе в
        # карточке стоит «readiness: high» вместо «Уровень готовности: Готов к назначению».
        schema = report.template.parameters_schema or {}
        context["parameters"] = [
            (
                schema.get(key, {}).get("label", key) if isinstance(schema.get(key), dict) else key,
                value,
            )
            for key, value in (report.parameters or {}).items()
        ]
        return context


@login_required
def report_download_view(request: AuthenticatedRequest, public_id) -> FileResponse:
    """Выдача файла выгрузки."""
    report = get_object_or_404(reports_for(request.user), public_id=public_id)

    if not report.is_ready or not report.file:
        raise Http404(_("Файл выгрузки ещё не сформирован"))

    try:
        handle = report.file.open("rb")
    except (FileNotFoundError, OSError) as error:  # pragma: no cover - сбой хранилища
        raise Http404(_("Файл выгрузки не найден")) from error

    report.download_count += 1
    report.save(update_fields=["download_count", "updated_at"])

    filename = f"{report.title}.{report.template.output_format}"
    content_type = content_type_for(report) or mimetypes.guess_type(filename)[0]

    response = FileResponse(handle, content_type=content_type)
    response["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(filename)}"
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
def delete_report_view(request: AuthenticatedRequest, public_id) -> HttpResponse:
    """Удаляет выгрузку вместе с файлом."""
    report = get_object_or_404(reports_for(request.user), public_id=public_id)

    is_owner = report.requested_by_id == request.user.pk
    if request.user.role not in PRIVILEGED_ROLES and not is_owner:
        raise PermissionDenied(_("Выгрузка принадлежит другому пользователю"))

    if request.method != "POST":
        return redirect(report.get_absolute_url())

    if report.file:
        report.file.delete(save=False)
    report.delete()

    messages.success(request, _("Выгрузка удалена"))
    return redirect("reporting:history")
