"""Маршруты раздела оценки."""

from __future__ import annotations

from django.urls import path

from apps.assessment import views

app_name = "assessment"

urlpatterns = [
    # -- Кампании (организатор) ---------------------------------------------
    path("", views.CampaignListView.as_view(), name="list"),
    path("campaigns/new/", views.CampaignCreateView.as_view(), name="campaign_create"),
    path(
        "campaigns/<uuid:public_id>/",
        views.CampaignDetailView.as_view(),
        name="campaign_detail",
    ),
    path(
        "campaigns/<uuid:public_id>/edit/",
        views.CampaignUpdateView.as_view(),
        name="campaign_edit",
    ),
    path(
        "campaigns/<uuid:public_id>/launch/",
        views.launch_campaign_view,
        name="campaign_launch",
    ),
    path(
        "campaigns/<uuid:public_id>/close/",
        views.close_campaign_view,
        name="campaign_close",
    ),
    path(
        "campaigns/<uuid:public_id>/sessions/",
        views.CampaignSessionsView.as_view(),
        name="campaign_sessions",
    ),
    path("sessions/", views.CampaignSessionsView.as_view(), name="sessions"),
    # -- Работа оценивающего -------------------------------------------------
    path("inbox/", views.SessionInboxView.as_view(), name="inbox"),
    path(
        "sessions/<uuid:public_id>/",
        views.SessionDetailView.as_view(),
        name="session_detail",
    ),
    path("sessions/<uuid:public_id>/fill/", views.fill_session_view, name="session_fill"),
    path(
        "sessions/<uuid:public_id>/approve/",
        views.approve_session_view,
        name="session_approve",
    ),
    path(
        "sessions/<uuid:public_id>/reject/",
        views.reject_session_view,
        name="session_reject",
    ),
    # -- Оценка 360° ---------------------------------------------------------
    path("respond/<str:token>/", views.respond_360_view, name="respond_360"),
    path("respond/<str:token>/decline/", views.decline_360_view, name="decline_360"),
    path(
        "campaigns/<uuid:public_id>/survey/<uuid:employee_id>/",
        views.Survey360View.as_view(),
        name="survey_360",
    ),
    # -- Результаты оцениваемого ---------------------------------------------
    path("my/", views.MyResultsView.as_view(), name="my_results"),
    # -- Тестирование --------------------------------------------------------
    path("tests/", views.TestListView.as_view(), name="tests"),
    path("tests/<int:pk>/", views.TestDetailView.as_view(), name="test_detail"),
    path("tests/<int:pk>/start/", views.start_attempt_view, name="test_start"),
    path("attempts/<uuid:public_id>/", views.take_attempt_view, name="attempt"),
    path(
        "attempts/<uuid:public_id>/finish/",
        views.finish_attempt_view,
        name="attempt_finish",
    ),
    path(
        "attempts/<uuid:public_id>/result/",
        views.AttemptResultView.as_view(),
        name="attempt_result",
    ),
    path("answers/<int:pk>/check/", views.check_answer_view, name="answer_check"),
    path(
        "attempts/<uuid:public_id>/publish/",
        views.publish_attempt_view,
        name="attempt_publish",
    ),
]
