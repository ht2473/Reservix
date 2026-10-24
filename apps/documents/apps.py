"""Конфигурация приложения «Документы»."""

from __future__ import annotations

from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class DocumentsConfig(AppConfig):
    """Файловое хранилище с версионированием и защищённой отдачей."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.documents"
    label = "documents"
    verbose_name = _("Документы")
