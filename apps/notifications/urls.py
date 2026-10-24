"""Маршруты раздела уведомлений."""

from __future__ import annotations

from django.urls import path

from apps.notifications import views

app_name = "notifications"

urlpatterns = [
    path("", views.NotificationListView.as_view(), name="list"),
    path("read-all/", views.mark_all_read_view, name="mark_all_read"),
    path("<int:pk>/open/", views.open_notification_view, name="open"),
    path("<int:pk>/read/", views.mark_read_view, name="mark_read"),
]
