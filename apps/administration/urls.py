"""Маршруты панели администрирования."""

from __future__ import annotations

from django.urls import path

from apps.administration import views

app_name = "administration"

urlpatterns = [
    # -- Сводка -------------------------------------------------------------
    path("", views.AdminIndexView.as_view(), name="index"),
    # -- Учётные записи -----------------------------------------------------
    path("users/", views.UserListView.as_view(), name="users"),
    path("users/new/", views.UserCreateView.as_view(), name="user_create"),
    path("users/<int:pk>/", views.UserDetailView.as_view(), name="user_detail"),
    path("users/<int:pk>/edit/", views.UserUpdateView.as_view(), name="user_edit"),
    path("users/<int:pk>/toggle/", views.toggle_user_active, name="user_toggle"),
    # -- Организационная структура ------------------------------------------
    path("org/", views.OrgStructureView.as_view(), name="org"),
    path("org/branches/<int:pk>/", views.BranchDetailView.as_view(), name="branch_detail"),
    path(
        "org/departments/<int:pk>/",
        views.DepartmentDetailView.as_view(),
        name="department_detail",
    ),
    path("org/positions/<int:pk>/", views.PositionDetailView.as_view(), name="position_detail"),
    # -- Справочники и расчёт -----------------------------------------------
    path("references/", views.ReferencesView.as_view(), name="references"),
    path("scoring/", views.ScoringView.as_view(), name="scoring"),
    # -- Служебные разделы --------------------------------------------------
    path("audit/", views.AuditLogView.as_view(), name="audit"),
    path("data/", views.SystemDataView.as_view(), name="data"),
    path("content/", views.ContentView.as_view(), name="content"),
    path("feedback/", views.FeedbackListView.as_view(), name="feedback"),
    path("feedback/<int:pk>/", views.FeedbackDetailView.as_view(), name="feedback_detail"),
]
