"""Маршруты раздела компетенций."""

from __future__ import annotations

from django.urls import path

from apps.competencies import views

app_name = "competencies"

urlpatterns = [
    # -- Справочник ---------------------------------------------------------
    path("", views.CompetencyListView.as_view(), name="index"),
    path("<int:pk>/", views.CompetencyDetailView.as_view(), name="competency_detail"),
    path(
        "categories/<int:pk>/",
        views.CategoryDetailView.as_view(),
        name="category_detail",
    ),
    # -- Модели требований --------------------------------------------------
    path("models/", views.CompetencyModelListView.as_view(), name="models"),
    path("models/<int:pk>/", views.CompetencyModelDetailView.as_view(), name="model_detail"),
    # -- Профили соответствия -----------------------------------------------
    path("profile/", views.MyCompetencyProfileView.as_view(), name="my_profile"),
    path(
        "profile/<uuid:public_id>/",
        views.EmployeeCompetencyProfileView.as_view(),
        name="employee_profile",
    ),
    path("matrix/", views.GapMatrixView.as_view(), name="matrix"),
]
