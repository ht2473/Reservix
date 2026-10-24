"""Маршруты раздела сотрудников."""

from __future__ import annotations

from django.urls import path

from apps.people import views

app_name = "people"

urlpatterns = [
    path("", views.EmployeeListView.as_view(), name="list"),
    path("team/", views.TeamView.as_view(), name="team"),
    path("<uuid:public_id>/", views.EmployeeDetailView.as_view(), name="employee_detail"),
    path("<uuid:public_id>/edit/", views.EmployeeCardUpdateView.as_view(), name="employee_edit"),
]
