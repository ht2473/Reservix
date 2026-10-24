"""Представления раздела уведомлений."""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import ListView

from apps.notifications.forms import NotificationFilterForm
from apps.notifications.models import Notification, NotificationKind
from apps.notifications.services import mark_all_read
from core.breadcrumbs import BreadcrumbTrail
from core.mixins import (
    AuthenticatedRequest,
    BreadcrumbMixin,
    FilterMixin,
    PageMetaMixin,
    WindowedPaginationMixin,
)
from core.permissions import current_user

#: Поводы, сгруппированные для сводки в шапке раздела. Пользователю важно
#: различать, что от него ждут действия, а что просто сообщают.
SUMMARY_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "reserve",
        (
            NotificationKind.RESERVE_INCLUDED,
            NotificationKind.RESERVE_EXCLUDED,
            NotificationKind.RESERVE_STATUS,
            NotificationKind.NOMINATION_SUBMITTED,
            NotificationKind.NOMINATION_APPROVED,
            NotificationKind.NOMINATION_REJECTED,
            NotificationKind.APPOINTMENT_MADE,
            NotificationKind.READINESS_CHANGED,
        ),
    ),
    (
        "development",
        (
            NotificationKind.PLAN_ASSIGNED,
            NotificationKind.ACTIVITY_DUE_SOON,
            NotificationKind.ACTIVITY_OVERDUE,
            NotificationKind.ACTIVITY_COMPLETED,
            NotificationKind.MENTORSHIP_ASSIGNED,
        ),
    ),
    (
        "assessment",
        (
            NotificationKind.ASSESSMENT_REQUESTED,
            NotificationKind.ASSESSMENT_COMPLETED,
            NotificationKind.CAMPAIGN_STARTED,
        ),
    ),
)


class NotificationListView(
    PageMetaMixin,
    BreadcrumbMixin,
    FilterMixin,
    WindowedPaginationMixin,
    ListView,
):
    """Лента уведомлений пользователя."""

    template_name = "notifications/list.html"
    model = Notification
    context_object_name = "notifications"
    paginate_by = 25
    filter_form_class = NotificationFilterForm
    page_section = "notifications"
    page_title = _("Уведомления")
    page_subtitle = _("События, о которых система известила вас")

    def get_breadcrumbs(self, trail: BreadcrumbTrail) -> BreadcrumbTrail:
        return trail.add(_("Уведомления"))

    def get_filter_form(self) -> NotificationFilterForm:
        """Форма отбора, связанная с данными даже при пустом запросе."""
        return self.filter_form_class(self.request.GET or {}, user=self.request.user)

    def get_queryset(self):
        queryset = (
            Notification.objects.for_user(current_user(self.request))
            .select_related("actor")
            .order_by("-created_at")
        )
        form = self.get_filter_form()
        if form.is_valid():
            return form.filter(queryset)
        return queryset

    def get_context_data(self, **kwargs) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)

        unread = Q(read_at__isnull=True)
        summary = Notification.objects.for_user(current_user(self.request)).aggregate(
            total=Count("id"),
            unread=Count("id", filter=unread),
            **{
                f"{name}_unread": Count("id", filter=unread & Q(kind__in=kinds))
                for name, kinds in SUMMARY_GROUPS
            },
        )
        context["summary"] = summary
        context["page_section_label"] = _("Личный кабинет")
        return context


@login_required
def open_notification_view(request: AuthenticatedRequest, pk: int) -> HttpResponse:
    """Отмечает уведомление прочитанным и ведёт к его предмету."""
    notification = get_object_or_404(Notification.objects.for_user(request.user), pk=pk)

    if request.method != "POST":
        return redirect("notifications:list")

    notification.mark_read()
    return redirect(notification.url or reverse("notifications:list"))


@login_required
def mark_read_view(request: AuthenticatedRequest, pk: int) -> HttpResponse:
    """Отмечает уведомление прочитанным, оставляя пользователя в ленте."""
    notification = get_object_or_404(Notification.objects.for_user(request.user), pk=pk)

    if request.method == "POST":
        notification.mark_read()

    return redirect(request.POST.get("next") or "notifications:list")


@login_required
def mark_all_read_view(request: AuthenticatedRequest) -> HttpResponse:
    """Отмечает прочитанными все уведомления пользователя."""
    if request.method != "POST":
        return redirect("notifications:list")

    count = mark_all_read(request.user)
    if count:
        messages.success(
            request,
            _("Отмечено прочитанными: %(count)s") % {"count": count},
        )
    else:
        messages.info(request, _("Непрочитанных уведомлений нет"))

    return redirect("notifications:list")
