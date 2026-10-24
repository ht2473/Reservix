"""Представления файлового хранилища."""

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

from apps.documents.forms import DocumentFilterForm, DocumentUploadForm
from apps.documents.models import Document
from apps.documents.services import (
    UPLOADER_ROLES,
    DocumentError,
    can_access,
    register_download,
    storage_statistics,
    store_document,
    verify_integrity,
    version_history,
    visible_documents,
)
from core.breadcrumbs import BreadcrumbTrail
from core.constants import PRIVILEGED_ROLES
from core.mixins import (
    AuthenticatedRequest,
    BreadcrumbMixin,
    FilterMixin,
    PageMetaMixin,
    RoleRequiredMixin,
    WindowedPaginationMixin,
)
from core.permissions import current_user

#: Тип содержимого для файлов, происхождение которых установить не удалось.
#: Браузер предложит сохранить файл, а не попытается его отобразить.
FALLBACK_CONTENT_TYPE = "application/octet-stream"


class DocumentSectionMixin(RoleRequiredMixin, PageMetaMixin, BreadcrumbMixin):
    """Общее поведение страниц хранилища."""

    page_section = "documents"

    def get_breadcrumbs(self, trail: BreadcrumbTrail) -> BreadcrumbTrail:
        trail.add(_("Документы"), "documents:list")
        title = getattr(self, "page_title", "")
        if title and self.request.resolver_match.url_name != "list":
            trail.add(str(title))
        return trail

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page_section_label"] = _("Документы")
        context["can_upload"] = self.request.user.role in UPLOADER_ROLES
        return context


class DocumentListView(
    DocumentSectionMixin,
    FilterMixin,
    WindowedPaginationMixin,
    ListView,
):
    """Перечень документов, доступных пользователю."""

    template_name = "documents/list.html"
    model = Document
    context_object_name = "documents"
    paginate_by = 25
    filter_form_class = DocumentFilterForm
    page_title = _("Документы")
    page_subtitle = _("Приказы, протоколы, сертификаты и прочие вложения")

    def get_queryset(self):
        queryset = visible_documents(current_user(self.request)).order_by("-created_at")
        form = self.get_filter_form()
        if form is not None and form.is_valid():
            queryset = form.filter(queryset)
        self._filter_form = form
        return queryset

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        if current_user(self.request).role in PRIVILEGED_ROLES:
            context["statistics"] = storage_statistics()
        return context


class DocumentDetailView(DocumentSectionMixin, DetailView):
    """Карточка документа: происхождение, срок действия, цепочка версий."""

    template_name = "documents/detail.html"
    model = Document
    context_object_name = "document"
    slug_field = "public_id"
    slug_url_kwarg = "public_id"
    page_title = _("Документ")

    def get_queryset(self):
        return Document.objects.with_uploader().select_related("employee", "superseded_by")

    def get_object(self, queryset=None) -> Document:
        document = super().get_object(queryset)
        if not can_access(current_user(self.request), document):
            raise PermissionDenied(_("Документ недоступен для вашей роли"))
        return document

    def get_page_title(self) -> str:
        return self.object.title

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        document: Document = self.object
        context["integrity_ok"] = verify_integrity(document)
        context["previous_versions"] = version_history(document)
        context["can_manage"] = (
            current_user(self.request).role in PRIVILEGED_ROLES
            or document.uploaded_by_id == self.request.user.pk
        )
        return context


class DocumentUploadView(DocumentSectionMixin, TemplateView):
    """Приём документа в хранилище."""

    template_name = "documents/upload.html"
    allowed_roles = UPLOADER_ROLES
    permission_denied_message = _("Загрузка документов доступна кадровой службе и руководителям")
    page_title = _("Загрузка документа")
    page_subtitle = _("Файл сохраняется под обезличенным именем и проверяется контрольной суммой")

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context.setdefault("form", DocumentUploadForm(user=current_user(self.request)))
        return context

    def post(self, request: AuthenticatedRequest, *args, **kwargs) -> HttpResponse:
        form = DocumentUploadForm(request.POST, request.FILES, user=request.user)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))

        data = form.cleaned_data
        try:
            document = store_document(
                data["file"],
                title=data["title"],
                uploader=request.user,
                kind=data["kind"],
                employee=data.get("employee"),
                access_level=data["access_level"],
                description=data.get("description", ""),
                valid_until=data.get("valid_until"),
                supersedes=data.get("supersedes"),
            )
        except DocumentError as error:
            messages.error(request, str(error))
            return self.render_to_response(self.get_context_data(form=form))

        messages.success(request, _("Документ принят в хранилище"))
        return redirect(document.get_absolute_url())


@login_required
def download_view(request: AuthenticatedRequest, public_id) -> FileResponse:
    """Защищённая выдача файла."""
    document = get_object_or_404(Document.objects.select_related("employee"), public_id=public_id)

    if not can_access(request.user, document):
        raise PermissionDenied(_("Документ недоступен для вашей роли"))

    try:
        handle = document.file.open("rb")
    except (FileNotFoundError, OSError) as error:  # pragma: no cover - сбой хранилища
        raise Http404(_("Файл документа не найден в хранилище")) from error

    register_download(document, request.user)

    filename = document.original_name or f"{document.public_id}.{document.extension}"
    content_type = (
        document.content_type_name or mimetypes.guess_type(filename)[0] or FALLBACK_CONTENT_TYPE
    )

    response = FileResponse(handle, content_type=content_type)
    response["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(filename)}"
    response["Content-Length"] = str(document.size_bytes)
    # Документ содержит персональные данные: промежуточным узлам его хранить
    # нельзя, а браузеру — только на время работы со страницей.
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
def delete_view(request: AuthenticatedRequest, public_id) -> HttpResponse:
    """Помечает документ удалённым."""
    document = get_object_or_404(Document.objects.select_related("employee"), public_id=public_id)

    is_owner = document.uploaded_by_id == request.user.pk
    if request.user.role not in PRIVILEGED_ROLES and not is_owner:
        raise PermissionDenied(_("Удаление доступно кадровой службе и автору загрузки"))

    if request.method != "POST":
        return redirect(document.get_absolute_url())

    document.delete(user=request.user)
    messages.success(request, _("Документ помечен удалённым и скрыт из списков"))
    return redirect("documents:list")
