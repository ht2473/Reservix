"""Маршруты раздела отчётности."""

from __future__ import annotations

from django.urls import path

from apps.reporting import views

app_name = "reporting"

urlpatterns = [
    # -- Каталог -------------------------------------------------------------
    path("", views.ReportIndexView.as_view(), name="index"),
    path("unit/", views.UnitReportsView.as_view(), name="unit"),
    path("mentor/", views.MentorReportsView.as_view(), name="mentor"),
    path("templates/<int:pk>/", views.ReportTemplateDetailView.as_view(), name="template_detail"),
    # -- Выгрузки ------------------------------------------------------------
    path("history/", views.GeneratedReportListView.as_view(), name="history"),
    path(
        "history/<uuid:public_id>/",
        views.GeneratedReportDetailView.as_view(),
        name="report_detail",
    ),
    path("history/<uuid:public_id>/download/", views.report_download_view, name="report_download"),
    path("history/<uuid:public_id>/delete/", views.delete_report_view, name="report_delete"),
]
