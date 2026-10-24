"""Маршруты раздела кадрового резерва."""

from __future__ import annotations

from django.urls import path

from apps.reserve import views

app_name = "reserve"

urlpatterns = [
    # -- Резерв (кадровая служба) -------------------------------------------
    path("", views.ReserveListView.as_view(), name="list"),
    path("reviews/", views.ReviewQueueView.as_view(), name="reviews"),
    path("candidates/", views.CandidateSearchView.as_view(), name="candidates"),
    path(
        "entries/<uuid:public_id>/",
        views.ReserveEntryDetailView.as_view(),
        name="entry_detail",
    ),
    path(
        "entries/<uuid:public_id>/edit/",
        views.ReserveEntryUpdateView.as_view(),
        name="entry_edit",
    ),
    path("entries/<uuid:public_id>/status/", views.change_status_view, name="entry_status"),
    path(
        "entries/<uuid:public_id>/readiness/",
        views.refresh_readiness_view,
        name="entry_readiness",
    ),
    path(
        "entries/<uuid:public_id>/review/",
        views.schedule_review_view,
        name="entry_review",
    ),
    # -- Рабочее место руководителя ------------------------------------------
    path("team/", views.TeamReserveView.as_view(), name="team"),
    path("approvals/", views.ApprovalsView.as_view(), name="approvals"),
    # -- Заявки на выдвижение -------------------------------------------------
    path("nominations/", views.NominationListView.as_view(), name="nominations"),
    path("nominations/new/", views.NominationCreateView.as_view(), name="nomination_create"),
    path(
        "nominations/<uuid:public_id>/",
        views.NominationDetailView.as_view(),
        name="nomination_detail",
    ),
    path(
        "nominations/<uuid:public_id>/decide/",
        views.decide_nomination_view,
        name="nomination_decide",
    ),
    path(
        "nominations/<uuid:public_id>/withdraw/",
        views.withdraw_nomination_view,
        name="nomination_withdraw",
    ),
    # -- Самовыдвижение --------------------------------------------------------
    path("apply/", views.ApplyView.as_view(), name="apply"),
    # -- Программы подготовки --------------------------------------------------
    path("groups/", views.ReserveGroupListView.as_view(), name="groups"),
    path("groups/<int:pk>/", views.ReserveGroupDetailView.as_view(), name="group_detail"),
]
