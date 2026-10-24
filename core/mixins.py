"""Примеси для представлений."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib import messages
from django.contrib.auth.mixins import AccessMixin, LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponseBase
from django.shortcuts import redirect
from django.utils.translation import gettext_lazy as _

from core.breadcrumbs import BreadcrumbTrail
from core.pagination import WindowedPaginator
from core.permissions import apply_employee_scope, apply_scope_by_path, has_role

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise

    from apps.accounts.models import User


class AuthenticatedRequest(HttpRequest):
    """Запрос от авторизованного пользователя."""

    user: User


class PageMetaMixin:
    """Метаданные страницы."""

    # Значения объявлены как StrOrPromise: заголовки задаются через gettext_lazy, и перевод
    # вычисляется в момент отрисовки, когда язык интерфейса уже определён промежуточным слоем
    # локализации.
    page_title: StrOrPromise = ""
    page_subtitle: StrOrPromise = ""
    page_description: StrOrPromise = ""
    page_section: str = ""

    def get_page_title(self) -> str:
        return str(self.page_title)

    def get_page_subtitle(self) -> str:
        return str(self.page_subtitle)

    def get_page_description(self) -> str:
        return str(self.page_description)

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context.setdefault("page_title", self.get_page_title())
        context.setdefault("page_subtitle", self.get_page_subtitle())
        context.setdefault("page_description", self.get_page_description())
        context.setdefault("page_section", self.page_section)
        return context


class BreadcrumbMixin:
    """Цепочка навигации страницы."""

    def get_breadcrumbs(self, trail: BreadcrumbTrail) -> BreadcrumbTrail:
        """Дополняет цепочку. Базовая реализация добавляет заголовок страницы."""
        title = getattr(self, "page_title", "")
        if title:
            trail.add(str(title))
        return trail

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        trail = self.get_breadcrumbs(BreadcrumbTrail(self.request))
        context["breadcrumbs"] = trail.resolve()
        return context


class RoleRequiredMixin(LoginRequiredMixin, AccessMixin):
    """Ограничение доступа к странице по роли."""

    allowed_roles: tuple[str, ...] = ()
    permission_denied_message: StrOrPromise = _("Раздел недоступен для вашей роли")

    def dispatch(self, request: HttpRequest, *args, **kwargs) -> HttpResponseBase:
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if self.allowed_roles and not has_role(request.user, *self.allowed_roles):
            raise PermissionDenied(str(self.permission_denied_message))
        return super().dispatch(request, *args, **kwargs)


class ScopedQuerySetMixin:
    """Ограничение выборки областью видимости пользователя."""

    scope_path: str = ""

    def get_queryset(self) -> QuerySet:
        queryset = super().get_queryset()
        user = self.request.user
        if not user.is_authenticated:
            return queryset.none()
        if self.scope_path:
            return apply_scope_by_path(queryset, user, prefix=self.scope_path)
        return apply_employee_scope(queryset, user)


class SortableMixin:
    """Сортировка списка по параметру адресной строки."""

    sort_param: str = "sort"
    sort_fields: dict[str, str] = {}
    default_sort: str = ""

    def get_current_sort(self) -> str:
        requested = self.request.GET.get(self.sort_param, "")
        key = requested.lstrip("-")
        if key in self.sort_fields:
            return requested
        return self.default_sort

    def apply_sort(self, queryset: QuerySet) -> QuerySet:
        current = self.get_current_sort()
        if not current:
            return queryset
        descending = current.startswith("-")
        field = self.sort_fields[current.lstrip("-")]
        return queryset.order_by(f"-{field}" if descending else field)

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        current = self.get_current_sort()
        context["current_sort"] = current
        context["sort_field"] = current.lstrip("-")
        context["sort_desc"] = current.startswith("-")
        return context


class FilterMixin:
    """Фильтрация списка формой."""

    filter_form_class: type[Any] | None = None

    def get_filter_form(self) -> Any | None:
        """Создаёт форму фильтров из параметров адреса."""
        if self.filter_form_class is None:
            return None
        return self.filter_form_class(self.request.GET or None, user=self.request.user)

    def get_queryset(self) -> QuerySet:
        return self.apply_filters(super().get_queryset())

    def apply_filters(self, queryset: QuerySet) -> QuerySet:
        """Отбирает записи формой фильтров; вызывается и из собственных get_queryset."""
        form = self.get_filter_form()
        if form is not None and form.is_valid():
            queryset = form.filter(queryset)
        self._filter_form = form
        return queryset

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        form = getattr(self, "_filter_form", None) or self.get_filter_form()
        context["filter_form"] = form
        context["has_active_filters"] = bool(
            {k: v for k, v in self.request.GET.items() if k not in {"page", "sort"} and v}
        )
        # Строка параметров без номера страницы: используется в ссылках
        # постраничной навигации, чтобы фильтры не сбрасывались.
        params = self.request.GET.copy()
        params.pop("page", None)
        context["querystring"] = params.urlencode()
        return context


class HtmxTemplateMixin:
    """Частичный ответ для запросов HTMX."""

    htmx_template_name: str = ""

    def get_template_names(self) -> list[str]:
        if self.htmx_template_name and getattr(self.request, "htmx", False):
            return [self.htmx_template_name]
        return super().get_template_names()


class SuccessMessageMixin:
    """Сообщение об успешном действии."""

    success_message: StrOrPromise = ""

    def get_success_message(self, cleaned_data: dict) -> str:
        if not self.success_message:
            return ""
        return str(self.success_message) % {**cleaned_data, "object": self.object}

    def form_valid(self, form):
        response = super().form_valid(form)
        message = self.get_success_message(form.cleaned_data)
        if message:
            messages.success(self.request, message)
        return response


class AuditedFormMixin:
    """Подстановка автора изменений в форму."""

    def form_valid(self, form):
        obj = form.instance
        if hasattr(obj, "created_by") and obj.pk is None:
            obj.created_by = self.request.user
        if hasattr(obj, "updated_by"):
            obj.updated_by = self.request.user
        return super().form_valid(form)


class OwnerOrPrivilegedMixin:
    """Доступ владельцу записи либо привилегированной роли."""

    owner_field: str = "employee__user"

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        from core.permissions import is_privileged

        if is_privileged(self.request.user):
            return obj

        owner = obj
        for part in self.owner_field.split("__"):
            owner = getattr(owner, part, None)
            if owner is None:
                break
        if owner != self.request.user:
            raise PermissionDenied(_("Запись принадлежит другому пользователю"))
        return obj


class RedirectToRoleHomeMixin:
    """Перенаправление авторизованного пользователя со страниц входа."""

    def dispatch(self, request: HttpRequest, *args, **kwargs) -> HttpResponseBase:
        if request.user.is_authenticated:
            from core.navigation import get_role_home_url

            return redirect(get_role_home_url(request.user))
        return super().dispatch(request, *args, **kwargs)


class WindowedPaginationMixin:
    """Постраничная навигация с ограниченным числом видимых номеров."""

    paginator_class = WindowedPaginator

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        paginator = context.get("paginator")
        page = context.get("page_obj")

        if paginator is not None and page is not None:
            context["visible_pages"] = paginator.visible_pages(page.number)
            context["total_count"] = paginator.count

        params = self.request.GET.copy()
        params.pop("page", None)
        context.setdefault("pagination_query", params.urlencode())
        return context
