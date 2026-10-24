"""Маршруты раздела развития."""

from __future__ import annotations

from django.urls import path

from apps.development import views

app_name = "development"

urlpatterns = [
    # -- Планы развития ------------------------------------------------------
    path("", views.PlanListView.as_view(), name="list"),
    path("plans/new/", views.PlanCreateView.as_view(), name="plan_create"),
    path("plans/<uuid:public_id>/", views.PlanDetailView.as_view(), name="plan_detail"),
    path("plans/<uuid:public_id>/edit/", views.PlanUpdateView.as_view(), name="plan_edit"),
    path(
        "plans/<uuid:public_id>/action/<str:action>/",
        views.plan_action_view,
        name="plan_action",
    ),
    path(
        "plans/<uuid:public_id>/complete/",
        views.plan_complete_view,
        name="plan_complete",
    ),
    path(
        "plans/<uuid:public_id>/activities/new/",
        views.ActivityCreateView.as_view(),
        name="activity_create",
    ),
    # -- Мероприятия ----------------------------------------------------------
    path("activities/", views.ActivityBoardView.as_view(), name="activities"),
    path(
        "activities/<uuid:public_id>/",
        views.ActivityDetailView.as_view(),
        name="activity_detail",
    ),
    path(
        "activities/<uuid:public_id>/edit/",
        views.ActivityUpdateView.as_view(),
        name="activity_edit",
    ),
    path(
        "activities/<uuid:public_id>/complete/",
        views.activity_complete_view,
        name="activity_complete",
    ),
    path(
        "activities/<uuid:public_id>/evidence/",
        views.evidence_add_view,
        name="evidence_add",
    ),
    path("evidence/<int:pk>/verify/", views.evidence_verify_view, name="evidence_verify"),
    # -- Личный план сотрудника ------------------------------------------------
    path("my/", views.MyPlanView.as_view(), name="my_plan"),
    # -- Наставничество --------------------------------------------------------
    path("mentorship/", views.MentorshipListView.as_view(), name="mentorship"),
    path(
        "mentorship/new/",
        views.MentorshipCreateView.as_view(),
        name="mentorship_create",
    ),
    path(
        "mentorship/<uuid:public_id>/",
        views.MentorshipDetailView.as_view(),
        name="mentorship_detail",
    ),
    path(
        "mentorship/<uuid:public_id>/meeting/",
        views.mentorship_meeting_view,
        name="mentorship_meeting",
    ),
    path(
        "mentorship/<uuid:public_id>/complete/",
        views.mentorship_complete_view,
        name="mentorship_complete",
    ),
    # -- Рабочее место наставника ----------------------------------------------
    path("mentees/", views.MenteeListView.as_view(), name="mentees"),
    path("mentees/plans/", views.MenteePlansView.as_view(), name="mentee_plans"),
    path("mentees/feedback/", views.MentorFeedbackView.as_view(), name="mentor_feedback"),
    # -- Библиотека развития ----------------------------------------------------
    path("resources/", views.ResourceListView.as_view(), name="resources"),
    path("resources/<int:pk>/", views.ResourceDetailView.as_view(), name="resource_detail"),
]
