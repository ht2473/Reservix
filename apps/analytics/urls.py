"""Маршруты аналитического ядра."""

from __future__ import annotations

from django.urls import path

from apps.analytics import views

app_name = "analytics"

urlpatterns = [
    # -- Результат -----------------------------------------------------------
    path("", views.AnalyticsOverviewView.as_view(), name="index"),
    path("ranking/", views.RankingView.as_view(), name="ranking"),
    path(
        "ranking/<uuid:public_id>/",
        views.EntryBreakdownView.as_view(),
        name="breakdown",
    ),
    # -- Основания: критерии и схемы -----------------------------------------
    path("criteria/", views.CriterionListView.as_view(), name="criteria"),
    path("criteria/sync/", views.ensure_criteria_view, name="criteria_sync"),
    path("criteria/<int:pk>/", views.CriterionDetailView.as_view(), name="criterion_detail"),
    path("schemes/", views.SchemeListView.as_view(), name="schemes"),
    path("schemes/new/", views.SchemeCreateView.as_view(), name="scheme_create"),
    path("schemes/recalculate/", views.recalculate_view, name="recalculate"),
    path("schemes/<int:pk>/", views.SchemeDetailView.as_view(), name="scheme_detail"),
    path("schemes/<int:pk>/edit/", views.SchemeUpdateView.as_view(), name="scheme_edit"),
    path("schemes/<int:pk>/weights/", views.expert_weights_view, name="scheme_weights"),
    path("schemes/<int:pk>/ahp/", views.ahp_view, name="scheme_ahp"),
    path("schemes/<int:pk>/entropy/", views.entropy_view, name="scheme_entropy"),
    # -- Проверка оснований ---------------------------------------------------
    path("comparison/", views.ComparisonView.as_view(), name="comparison"),
    path("stability/", views.StabilityView.as_view(), name="stability"),
    # -- Матрица 9-box --------------------------------------------------------
    path("nine-box/", views.NineBoxView.as_view(), name="ninebox"),
    path("nine-box/placements/", views.PlacementListView.as_view(), name="placements"),
    path("nine-box/placements/new/", views.PlacementCreateView.as_view(), name="placement_create"),
    path("nine-box/placements/auto/", views.auto_place_view, name="placement_auto"),
    path("nine-box/placements/<int:pk>/calibrate/", views.calibrate_view, name="calibrate"),
    # -- Сессии калибровки ----------------------------------------------------
    path("calibrations/", views.CalibrationListView.as_view(), name="calibrations"),
    path(
        "calibrations/new/",
        views.CalibrationCreateView.as_view(),
        name="calibration_create",
    ),
    path(
        "calibrations/<uuid:public_id>/",
        views.CalibrationDetailView.as_view(),
        name="calibration_detail",
    ),
    path(
        "calibrations/<uuid:public_id>/edit/",
        views.CalibrationUpdateView.as_view(),
        name="calibration_edit",
    ),
    path(
        "calibrations/<uuid:public_id>/close/",
        views.close_calibration_view,
        name="calibration_close",
    ),
]
