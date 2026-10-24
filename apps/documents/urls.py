"""Маршруты файлового хранилища."""

from __future__ import annotations

from django.urls import path

from apps.documents import views

app_name = "documents"

urlpatterns = [
    path("", views.DocumentListView.as_view(), name="list"),
    path("upload/", views.DocumentUploadView.as_view(), name="upload"),
    path("<uuid:public_id>/", views.DocumentDetailView.as_view(), name="detail"),
    path("<uuid:public_id>/download/", views.download_view, name="download"),
    path("<uuid:public_id>/delete/", views.delete_view, name="delete"),
]
