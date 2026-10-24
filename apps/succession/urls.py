"""Маршруты раздела преемственности."""

from __future__ import annotations

from django.urls import path

from apps.succession import views

app_name = "succession"

urlpatterns = [
    # -- Обзор ---------------------------------------------------------------
    path("", views.SuccessionOverviewView.as_view(), name="list"),
    path("coverage/", views.CoverageView.as_view(), name="coverage"),
    # -- Критичные должности --------------------------------------------------
    path("key-positions/", views.KeyPositionListView.as_view(), name="key_positions"),
    path(
        "key-positions/new/",
        views.KeyPositionCreateView.as_view(),
        name="key_position_create",
    ),
    path(
        "key-positions/sync/",
        views.sync_key_positions_view,
        name="key_positions_sync",
    ),
    path(
        "key-positions/<int:pk>/",
        views.KeyPositionDetailView.as_view(),
        name="key_position_detail",
    ),
    path(
        "key-positions/<int:pk>/edit/",
        views.KeyPositionUpdateView.as_view(),
        name="key_position_edit",
    ),
    path("key-positions/<int:pk>/plan/", views.create_plan_view, name="plan_create"),
    # -- Планы преемственности -------------------------------------------------
    path("plans/", views.SuccessionPlanListView.as_view(), name="plans"),
    path(
        "plans/<uuid:public_id>/",
        views.SuccessionPlanDetailView.as_view(),
        name="plan_detail",
    ),
    path(
        "plans/<uuid:public_id>/edit/",
        views.SuccessionPlanUpdateView.as_view(),
        name="plan_edit",
    ),
    path(
        "plans/<uuid:public_id>/action/<str:action>/",
        views.plan_action_view,
        name="plan_action",
    ),
    path(
        "plans/<uuid:public_id>/successors/",
        views.add_successor_view,
        name="slate_add",
    ),
    path(
        "plans/<uuid:public_id>/successors/order/",
        views.reorder_slate_view,
        name="slate_order",
    ),
    path("successors/<int:pk>/remove/", views.remove_successor_view, name="slate_remove"),
    # -- Назначения ------------------------------------------------------------
    path("appointments/", views.AppointmentListView.as_view(), name="appointments"),
    path(
        "appointments/new/",
        views.AppointmentCreateView.as_view(),
        name="appointment_create",
    ),
    path(
        "appointments/<uuid:public_id>/",
        views.AppointmentDetailView.as_view(),
        name="appointment_detail",
    ),
]
