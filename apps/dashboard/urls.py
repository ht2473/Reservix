"""Маршруты стартовых страниц рабочих мест."""

from __future__ import annotations

from django.urls import path

from apps.dashboard import views

app_name = "dashboard"

urlpatterns = [
    path("hr/", views.HrDashboardView.as_view(), name="hr"),
    path("manager/", views.ManagerDashboardView.as_view(), name="manager"),
    path("my/", views.EmployeeDashboardView.as_view(), name="employee"),
]
