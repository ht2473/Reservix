"""Маршруты публичной части."""

from __future__ import annotations

from django.urls import path

from apps.portal import views

app_name = "portal"

urlpatterns = [
    path("", views.HomeView.as_view(), name="home"),
    path("about/", views.AboutView.as_view(), name="about"),
    # -- Методика и инструменты ------------------------------------------------
    path("methodology/", views.MethodologyView.as_view(), name="methodology"),
    path("competencies/", views.CompetenciesView.as_view(), name="competencies"),
    path("readiness/", views.ReadinessView.as_view(), name="readiness"),
    path("nine-box/", views.NineBoxView.as_view(), name="ninebox"),
    # -- Нормативная база --------------------------------------------------------
    path("regulations/", views.RegulationsView.as_view(), name="regulations"),
    path(
        "regulations/<slug:code>.docx",
        views.regulation_download_view,
        name="regulation_download",
    ),
    # -- База знаний -------------------------------------------------------------
    path("knowledge/", views.ArticleListView.as_view(), name="articles"),
    path("knowledge/<slug:slug>/", views.ArticleDetailView.as_view(), name="article_detail"),
    path("glossary/", views.GlossaryView.as_view(), name="glossary"),
    path("faq/", views.FaqView.as_view(), name="faq"),
    path("api/", views.ApiDocsView.as_view(), name="api_docs"),
    # -- Обратная связь ----------------------------------------------------------
    path("contacts/", views.ContactsView.as_view(), name="contacts"),
    path("feedback/", views.FeedbackView.as_view(), name="feedback"),
    path("feedback/sent/", views.FeedbackSentView.as_view(), name="feedback_sent"),
    # Обобщённый маршрут статических страниц — последним в списке.
    path("page/<slug:slug>/", views.StaticPageView.as_view(), name="page"),
]
